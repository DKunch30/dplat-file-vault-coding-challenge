import hashlib
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db.models import Q
from django.db import transaction
from django.conf import settings
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
    # Standard DRF CRUD; custom permission + throttle applied globally.
    queryset = File.objects.all().order_by('-uploaded_at')
    serializer_class = FileSerializer
    permission_classes = [HasUserIdHeader]
    throttle_classes = [UserIdRateThrottle]


    # if we wanted throttle to be end-point specific, we could implement something like:
    # def get_throttles(self):
    #     # Map DRF actions to throttle scopes
    #     if self.action in ("list", "retrieve"):
    #         self.throttle_scope = "files.read"
    #     elif self.action == "create":
    #         self.throttle_scope = "files.upload"
    #     elif self.action == "destroy":
    #         self.throttle_scope = "files.delete"
    #     else:
    #         self.throttle_scope = "files.read"  # sensible default
    #     return super().get_throttles()


    # get_queryset applies all search/filters efficiently.
    # This is why the list and retrieve endpoints are implicitly user-scoped.
    # DB indexes directly support these filters.
    # Example Call on search, min_size and file_type: 
    # GET /api/files/?search=report&min_size=10000&file_type=application/pdf
    # -H "UserId: u1"
    # ---------- Queryset scoping + filters ----------
    def get_queryset(self):
        # Getting requesting user's ID from header, so user only sees their own files
        user_id = _get_user_id(self.request)

        # Right now qs = all of this user’s files, sorted by newest upload date
        # Since created indexes on user_id and uploaded_at, query runs efficiently even w/ thousands of records.
        qs = File.objects.filter(user_id=user_id).order_by('-uploaded_at')

        # query_params is DRF exposing ?key=value parts of a URL
        qp = self.request.query_params

        # looking for search in URL by filename (case-insensitive)
        # With case-insensitive (_icontains), search=doc matches Document.pdf, myDOC.txt, etc.
        search = qp.get('search')
        if search:
            qs = qs.filter(original_filename__icontains=search)

        # filter by MIME type, Using a direct equality filter for precision (example, all pdfs by user)
        file_type = qp.get('file_type')
        if file_type:
            qs = qs.filter(file_type=file_type)

        # Parses the numeric filters (if present) and converts them to integers.
        # numeric size range (example, all files over 1MB by user)
        try:
            min_size = int(qp.get('min_size')) if qp.get('min_size') else None
            max_size = int(qp.get('max_size')) if qp.get('max_size') else None
        except ValueError:
            min_size = max_size = None

        # Adds a numeric range filter for file size (in bytes).
        # __gte = “greater than or equal to,” __lte = “less than or equal to.” 
        # Returns files between sizes, if provided in Get call
        if min_size is not None:
            qs = qs.filter(size__gte=min_size)
        if max_size is not None:
            qs = qs.filter(size__lte=max_size)

        # Helper function that converts an ISO 8601 date string (e.g. "2025-10-04T15:00:00")
        # into a timezone-aware Python datetime object.
        # date range (parse ISO with/without tz; make aware if needed)
        def _parse_iso(dt_str):
            if not dt_str:
                return None
            dt = parse_datetime(dt_str)
            if dt and timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt

        # Allows users to get files uploaded within a certain time range.
        # Returns files between dates, if provided in Get call
        start_dt = _parse_iso(qp.get('start_date'))
        end_dt = _parse_iso(qp.get('end_date'))

        if start_dt:
            qs = qs.filter(uploaded_at__gte=start_dt)
        if end_dt:
            qs = qs.filter(uploaded_at__lte=end_dt)

        # At this point, qs is the final filtered list of File objects (like several ANDs in WHERE clause of a SQL Query).
        # DRF automatically serializes it into JSON using the FileSerializer.
        return qs

    # create computes SHA-256, performs dedup, and enforces dedup-aware quotas (10 MB default).
    # ---------- Create with dedup + storage quota enforcement ----------
    def create(self, request, *args, **kwargs):
        user_id = _get_user_id(request)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)

        # compute metadata/basic fields
        file_hash = _compute_sha256(file_obj)
        file_size = file_obj.size
        file_type = getattr(file_obj, 'content_type', '') or ''
        original_filename = file_obj.name

        # Quota check (dedup-aware): adding a duplicate doesn't consume extra storage
        # After checking Django settings for a quota, defaults to 10 MB.
        quota_mb = int(getattr(settings, 'FILE_VAULT', {}).get('STORAGE_QUOTA_MB', 10))
        # Converts MB to bytes for comparison (1 MB = 1,048,576 bytes)
        # only accounts for user’s total unique file data 
        quota_bytes = quota_mb * 1024 * 1024

        # Get all files for THIS user (only file_hash and size)
        user_files = File.objects.filter(user_id=user_id).only('file_hash', 'size')
        # uses set to get sum of sizes of UNIQUE hashes for THIS user
        seen = set()
        # Result: total_storage_used = bytes of unique data this user has stored.
        total_storage_used = 0
        for f in user_files:
            if f.file_hash not in seen:
                seen.add(f.file_hash)
                total_storage_used += f.size

        # If the user already uploaded a file with the same hash, 
        # it doesn’t consume more space (additional_bytes = 0).
        # Otherwise, the file’s size is added.
        # Checking if this latest upload will exceed quota.
        user_already_has_hash = file_hash in seen
        additional_bytes = 0 if user_already_has_hash else file_size

        # If total_storage_used + additional_bytes exceeds quota_bytes, 
        # we return a 429 Too Many Requests response.
        if total_storage_used + additional_bytes > quota_bytes:
            return Response({'detail': 'Storage Quota Exceeded'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        # If an original (physical) copy already exists for that hash (by any user), 
        # we create a reference and point new_row.file.name to the same file path.
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
            # original: save the actual content, creating original row and saving uploaded bytes
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
    # Called at DELETE /api/files/<file_id>/ 
    # -H "UserId: u1"
    # ---------- Promotion + safe physical deletion ----------
    def destroy(self, request, *args, **kwargs):
        # Ensures that all database operations inside the block either complete successfully together or roll back entirely if anything fails.
        # Prevents race conditions when multiple users might delete related files simultaneously.
        # This is important because we might be promoting one record and deleting another in the same block.
        with transaction.atomic():
            # fetches the specific File row to be deleted (based on the URL’s id).
            instance = self.get_object()
            # file_hash is saved locally for later, since the record will soon be gone from the DB.
            file_hash = instance.file_hash

            # If deleting an original, promote one reference to be the new original
            if not instance.is_reference:
                ref = (
                    File.objects
                    # Locks selected rows in the database until the transaction completes.
                    # Prevents race conditions (e.g., two users deleting at once).
                    # Ensures two references aren’t promoted at the same time.
                    .select_for_update()
                    # Finds the first reference file pointing to this original.
                    .filter(original_file=instance)
                    .first()
                )
                # If we found a reference, we promote it as the new original (is_reference=False).
                # Clears its original_file foreign key (None), since it now is the source file.
                # update_fields tells Django to update only those two columns for efficiency.
                if ref:
                    ref.is_reference = False
                    ref.original_file = None
                    ref.save(update_fields=['is_reference', 'original_file'])

            # Calls DRF’s built-in destroy() to actually remove this record from the database.
            # This doesn’t touch the physical file yet — only the DB record is deleted.
            super().destroy(request, *args, **kwargs)

            # If no rows remain for that hash, delete the on-disk (physical) file
            # After deleting the DB row, checking if any other file records exist for the same file_hash.
            # If none exist then this was the last copy (no more references).
            # Safe to delete the file itself from disk.
            if not File.objects.filter(file_hash=file_hash).exists():
                try:
                    # tells Django’s storage backend (e.g., local media folder, S3, etc.) to remove the file.
                    instance.file.storage.delete(instance.file.name)
                except Exception:
                    pass

        # HTTP 204 = “No Content” — standard response for successful deletions.
        return Response(status=status.HTTP_204_NO_CONTENT)


    # storage_stats delivers total/original/savings per spec.
    # @action is a DRF decorator that creates a custom route within a ViewSet.
    # detail=False means this action doesn’t act on a single record 
    # (like GET /files/<id>/); it’s a collection-level action.
    # url_path='storage_stats', makes the route available at: GET /api/files/storage_stats/
    # methods=['get'] means it only responds to GET requests.
    # ---------- Utility endpoint ----------
    @action(detail=False, methods=['get'], url_path='storage_stats')
    def storage_stats(self, request):
        user_id = _get_user_id(request)

        # Gets all file records for the given user. .only('file_hash', 'size') fetches 
        # just the two columns we need for calculation (efficient and memory-light).
        # Each record represents either an original or reference file.
        qs = File.objects.filter(user_id=user_id).only('file_hash', 'size')

        # original_storage_used: sum of all sizes the user uploaded (including duplicates)
        original_storage_used = sum(f.size for f in qs)

        # total_storage_used: sum of unique-hash sizes (deduped for this user)
        # Using a dictionary to count only unique file hashes.
        seen = {}
        for f in qs:
            seen.setdefault(f.file_hash, f.size)
        total_storage_used = sum(seen.values())

        # savings: calculated difference between original storage utilized by uploads and total storage actually used
        # max(..., 0) ensures it never goes negative (in case of rounding errors or empty data).
        savings = max(original_storage_used - total_storage_used, 0)
        # savings_percentage: calculated percentage of savings
        pct = (savings / original_storage_used * 100.0) if original_storage_used else 0.0

        # Returns a JSON response with the calculated statistics exactly how shown in README.md
        return Response({
            'user_id': user_id,
            'total_storage_used': total_storage_used,
            'original_storage_used': original_storage_used,
            'storage_savings': savings,
            'savings_percentage': round(pct, 2),
        })

    # The file_types endpoint returns all distinct MIME types of files uploaded by the current user.
    # Called like so:
    # GET /api/files/file_types/
    # -H "UserId: u1"
    # ---------- Utility endpoint ----------
    @action(detail=False, methods=['get'], url_path='file_types')
    def file_types(self, request):
        user_id = _get_user_id(request)
        types = (
            File.objects.filter(user_id=user_id)
            # Passing an empty order_by() clears any default ordering (like -uploaded_at).
            # This allows .distinct() to work properly at the database level — some databases 
            # (like if we were to use PostgreSQL) require a clean ordering to return unique rows.
            .order_by()
            # Retrieves just the file_type column from each matching record.
            # flat=True means the result will be a simple list of values instead of tuples.
            .values_list('file_type', flat=True)
            # Returns only unique values, removing duplicates.
            .distinct()
        )

        # Converts the QuerySet (which is iterable) into a plain Python list.
        # Wraps it in a DRF Response object, which automatically serializes it to JSON.
        return Response(list(types))
