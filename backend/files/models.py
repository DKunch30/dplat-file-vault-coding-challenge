from django.db import models
import uuid
import os

def file_upload_path(instance, filename):
    """
    Generate a collision-resistant path inside MEDIA_ROOT/uploads/ for the
    physical file. We keep the user-visible name in `original_filename`,
    and store a UUID filename on disk to avoid collisions and weird characters.
    """
    ext = filename.split('.')[-1] if '.' in filename else ''
    filename = f"{uuid.uuid4()}.{ext}" if ext else str(uuid.uuid4())
    return os.path.join('uploads', filename)

class File(models.Model):
    """
    Deduplicated file entry.

    - Originals (is_reference=False) store bytes on disk (`file`); `original_file` is NULL.
    - References (is_reference=True) reuse the same on-disk file; `original_file` points to the original row.
    - All rows with identical content share the same `file_hash`.

    Common filters indexed: file_hash, user_id, file_type, uploaded_at.
    """
    # Primary key: UUID prevents guessable IDs in API URLs.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Physical storage + metadata captured at upload time
    file = models.FileField(upload_to=file_upload_path)  # on-disk path (MEDIA_ROOT/uploads/<uuid>.<ext>)
    original_filename = models.CharField(max_length=255) # user-visible/original file name
    file_type = models.CharField(max_length=100, db_index=True) # MIME type (e.g., "application/pdf")
    size = models.BigIntegerField() # file size in bytes
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True) # when the file was uploaded

    # Dedup (content identity) + ownership
    # The user_id helps us to filter and limit by owner
    user_id = models.CharField(max_length=64, db_index=True)
    # The file_hash helps us enable fast dedup look up (SHA-256 hex of content)
    file_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 hex
    # is_reference and original_file helps cleanly represent dedup so only one physical copy exists
    is_reference = models.BooleanField(default=False) # True if this is a reference file (points to another file)
    original_file = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        related_name='references', # original.references -> all references pointing to it
        on_delete=models.SET_NULL, # if original is deleted, references are kept (we promote one to new original)
    )

    class Meta:
        # default ordering: show newest uploads first
        ordering = ['-uploaded_at']
        # Indexes optimize the list filters and hash lookups
        indexes = [
            models.Index(fields=['file_hash']),
            models.Index(fields=['user_id', 'file_hash']),
            models.Index(fields=['file_type']),
            models.Index(fields=['uploaded_at']),
        ]
        

    @property
    def reference_count(self):
        """
        Returns the total number of references to this file (excluding itself).
        For original files: counts all references pointing to it (excluding the original)
        For reference files: counts all references to the original file (excluding the original)
        TODO: Maybe I should ask during next interview if want to include the original file in the count
        """
        if self.is_reference and self.original_file:
            # For references, count references to the original file 
            return self.original_file.references.count() # + 1 # need to figure out if should include original
        else:
            # For original files, count references pointing to this file 
            return self.references.count() #+ 1 # need to figure out if should include original

    def __str__(self):
        return f"{self.original_filename} ({self.user_id})"
