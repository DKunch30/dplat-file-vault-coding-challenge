import hashlib
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db.models import Q
from django.db import transaction
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action

from .models import File
from .serializers import FileSerializer
from .permissions import HasUserIdHeader
from .throttling import UserIdRateThrottle

def _get_user_id(request):
    uid = request.headers.get('UserId') or request.META.get('HTTP_USERID')
    if not uid:
        # Permission class should already enforce; this is a safety net
        raise ValueError("Missing UserId header")
    return uid

def _compute_sha256(dj_file):
    hasher = hashlib.sha256()
    for chunk in dj_file.chunks():
        hasher.update(chunk)
    dj_file.seek(0)  # important: reset pointer so Django can save it
    return hasher.hexdigest()

class FileViewSet(viewsets.ModelViewSet):
    queryset = File.objects.all().order_by('-uploaded_at')
    serializer_class = FileSerializer
    permission_classes = [HasUserIdHeader]
    throttle_classes = [UserIdRateThrottle]

    # get_queryset applies all search/filters efficiently.
    # ---------- Queryset scoping + filters ----------
    def get_queryset(self):
        user_id = _get_user_id(self.request)
        qs = File.objects.filter(user_id=user_id).order_by('-uploaded_at')

        qp = self.request.query_params

        # search by filename (case-insensitive)
        search = qp.get('search')
        if search:
            qs = qs.filter(original_filename__icontains=search)

        # filter by MIME type
        file_type = qp.get('file_type')
        if file_type:
            qs = qs.filter(file_type=file_type)

        # size range
        try:
            min_size = int(qp.get('min_size')) if qp.get('min_size') else None
            max_size = int(qp.get('max_size')) if qp.get('max_size') else None
        except ValueError:
            min_size = max_size = None

        if min_size is not None:
            qs = qs.filter(size__gte=min_size)
        if max_size is not None:
            qs = qs.filter(size__lte=max_size)

        # date range (ISO 8601, with or without tz)
        def _parse_iso(dt_str):
            if not dt_str:
                return None
            dt = parse_datetime(dt_str)
            if dt and timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt

        start_dt = _parse_iso(qp.get('start_date'))
        end_dt = _parse_iso(qp.get('end_date'))

        if start_dt:
            qs = qs.filter(uploaded_at__gte=start_dt)
        if end_dt:
            qs = qs.filter(uploaded_at__lte=end_dt)

        return qs

    # create computes SHA-256, performs dedup, and enforces dedup-aware quotas (10 MB default).
    # ---------- Create with dedup + storage quota enforcement ----------
    def create(self, request, *args, **kwargs):
        user_id = _get_user_id(request)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        file_hash = _compute_sha256(file_obj)
        file_size = file_obj.size
        file_type = getattr(file_obj, 'content_type', '') or ''
        original_filename = file_obj.name

        # Quota check (dedup-aware): adding a duplicate doesn't consume extra storage
        from django.conf import settings
        quota_mb = int(getattr(settings, 'FILE_VAULT', {}).get('STORAGE_QUOTA_MB', 10))
        quota_bytes = quota_mb * 1024 * 1024

        user_files = File.objects.filter(user_id=user_id).only('file_hash', 'size')
        # sum of sizes of unique hashes for THIS user
        seen = set()
        total_storage_used = 0
        for f in user_files:
            if f.file_hash not in seen:
                seen.add(f.file_hash)
                total_storage_used += f.size

        # Will this upload add bytes for THIS user?
        user_already_has_hash = file_hash in seen
        additional_bytes = 0 if user_already_has_hash else file_size

        if total_storage_used + additional_bytes > quota_bytes:
            return Response({'detail': 'Storage Quota Exceeded'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # If an original (physical) copy already exists for that hash (by any user), we create a reference and point new_row.file.name to the same file path.
        # If not, we store a new original on disk.
        existing_original = File.objects.filter(file_hash=file_hash, is_reference=False).first()
        # Create row (original or reference)
        if existing_original:
            # reference: share the same physical file; do not re-upload
            # Create a new File instance that references the existing file
            new_row = File(
                original_filename=original_filename,
                file_type=file_type,
                size=file_size,
                user_id=user_id,
                file_hash=file_hash,
                is_reference=True,
                original_file=existing_original,
            )
            # Manually set the file field to point to the same file as the original
            new_row.file.name = existing_original.file.name
            new_row.save()
        else:
            # original: save the actual content
            new_row = File.objects.create(
                file=file_obj,
                original_filename=original_filename,
                file_type=file_type,
                size=file_size,
                user_id=user_id,
                file_hash=file_hash,
                is_reference=False,
                original_file=None,
            )

        serializer = self.get_serializer(new_row)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    # destroy promotes a reference if needed and only deletes the on-disk file when no rows remain for that hash.
    # ---------- Promotion + safe physical deletion ----------
    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            file_hash = instance.file_hash

            if not instance.is_reference:
                ref = (
                    File.objects
                    .select_for_update()
                    .filter(original_file=instance)
                    .first()
                )
                if ref:
                    ref.is_reference = False
                    ref.original_file = None
                    ref.save(update_fields=['is_reference', 'original_file'])

            super().destroy(request, *args, **kwargs)

            if not File.objects.filter(file_hash=file_hash).exists():
                try:
                    instance.file.storage.delete(instance.file.name)
                except Exception:
                    pass

        return Response(status=status.HTTP_204_NO_CONTENT)


    # storage_stats delivers total/original/savings per spec.
    # ---------- Utility endpoints ----------
    @action(detail=False, methods=['get'], url_path='storage_stats')
    def storage_stats(self, request):
        user_id = _get_user_id(request)
        qs = File.objects.filter(user_id=user_id).only('file_hash', 'size')

        # original_storage_used: sum of all sizes the user uploaded (including duplicates)
        original_storage_used = sum(f.size for f in qs)

        # total_storage_used: sum of unique-hash sizes (deduped for this user)
        seen = {}
        for f in qs:
            seen.setdefault(f.file_hash, f.size)
        total_storage_used = sum(seen.values())

        savings = max(original_storage_used - total_storage_used, 0)
        pct = (savings / original_storage_used * 100.0) if original_storage_used else 0.0

        return Response({
            'user_id': user_id,
            'total_storage_used': total_storage_used,
            'original_storage_used': original_storage_used,
            'storage_savings': savings,
            'savings_percentage': round(pct, 2),
        })

    # file_types returns distinct MIME types.
    @action(detail=False, methods=['get'], url_path='file_types')
    def file_types(self, request):
        user_id = _get_user_id(request)
        types = (
            File.objects.filter(user_id=user_id)
            .order_by()
            .values_list('file_type', flat=True)
            .distinct()
        )
        return Response(list(types))
