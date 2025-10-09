from django.db import models
import uuid
import os

def file_upload_path(instance, filename):
    """Generate file path for new file upload"""
    ext = filename.split('.')[-1] if '.' in filename else ''
    filename = f"{uuid.uuid4()}.{ext}" if ext else str(uuid.uuid4())
    return os.path.join('uploads', filename)

class File(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Physical storage + metadata
    file = models.FileField(upload_to=file_upload_path)
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=100, db_index=True)
    size = models.BigIntegerField()
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Dedup + ownership
    # The user_id helps us to filter and limit by owner
    user_id = models.CharField(max_length=64, db_index=True)
    # The file_hash helps us enable fast dedup look up
    file_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 hex
    # is_reference and original_file helps cleanly represent dedup so only one physical copy exists
    is_reference = models.BooleanField(default=False)
    original_file = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        related_name='references',
        on_delete=models.SET_NULL,
    )

    class Meta:
        # Indexes optimize the list filters and hash lookups
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['file_hash']),
            models.Index(fields=['user_id', 'file_hash']),
            models.Index(fields=['file_type']),
            models.Index(fields=['uploaded_at']),
        ]

    @property
    def reference_count(self):
        """
        Returns the total number of references to this file (including itself).
        For original files: counts all references pointing to it + 1 (itself)
        For reference files: counts all references to the original file + 1 (the original)
        """
        if self.is_reference and self.original_file:
            # For references, count references to the original file + the original itself
            return self.original_file.references.count() # + 1 # need to figure out if should include original
        else:
            # For original files, count references pointing to this file + itself
            return self.references.count() #+ 1 # need to figure out if should include original

    def __str__(self):
        return f"{self.original_filename} ({self.user_id})"
