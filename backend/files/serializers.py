'''
     Serializes File including computed reference_count, and exposes original_file as a UUID (not nested).
     NOTE: `file` is a DRF FileField; responses include the served URL (when MEDIA_URL is configured).
     `original_file` is output-only to keep the dedup/promotion logic server-controlled.
'''

from rest_framework import serializers
from .models import File

class FileSerializer(serializers.ModelSerializer):
    # Exposes fields required by the API contract (including reference_count, is_reference, original_file).
    # original_file is presented as the FK id.
    reference_count = serializers.SerializerMethodField()
    original_file = serializers.UUIDField(source='original_file_id', read_only=True)

    class Meta:
        # Clients can only upload the file; other fields are server-controlled.
        model = File
        fields = [
            'id', 'file', 'original_filename', 'file_type', 'size', 'uploaded_at',
            'user_id', 'file_hash', 'reference_count', 'is_reference', 'original_file'
        ]
        read_only_fields = [
            'id', 'uploaded_at', 'user_id', 'file_hash', 'reference_count', 'is_reference', 'original_file'
        ]

    def get_reference_count(self, obj):
        """
        Number of references pointing to this file's original (system-wide).
        Uses the model's reference_count property for consistent logic.
        """
        return obj.reference_count
