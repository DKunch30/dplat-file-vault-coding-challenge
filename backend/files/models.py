'''
    Represents a file entry with deduplication. Two kinds of rows:
        Original (is_reference=False): owns the physical bytes on disk.
        Reference (is_reference=True): points to the original; no bytes duplicated.
    Invariant: if is_reference==True, original_file MUST be non-null.
    Application logic guarantees only one physical copy per unique file_hash.
    We intentionally do not put a uniqueness constraint on file_hash because
    multiple rows (original + many references) share the same hash.
'''

from django.db import models
import uuid
import os

def file_upload_path(instance, filename):
    """
    Generate a collision-resistant path inside MEDIA_ROOT/uploads/ for the
    physical file. We keep the user-visible name in `original_filename`,
    and store a UUID filename on disk to avoid collisions and weird characters.
    Store physical file as uploads/<uuid>.<ext>
    """
    ext = filename.split('.')[-1] if '.' in filename else ''
    filename = f"{uuid.uuid4()}.{ext}" if ext else str(uuid.uuid4())
    return os.path.join('uploads', filename)

class File(models.Model):
    """
    Deduplicated file entry.

    - Originals (is_reference=False) store bytes on disk (`file`); `original_file` is NULL.
    - References (is_reference=True) reuse the same on-disk file; `original_file` points to the original row.
    - All rows with identical content share the same SHA-256 hash `file_hash`.

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
    # The user_id helps us to filter and limit by owner (faster lookups like hash table)
    user_id = models.CharField(max_length=64, db_index=True) # create database index on column, jump directly to matching rows instead of scanning entire table
    # The file_hash helps us enable fast dedup look up (SHA-256 hex of content)
    file_hash = models.CharField(max_length=64, db_index=True) 
    # is_reference and original_file helps cleanly represent dedup so only one physical copy exists
    is_reference = models.BooleanField(default=False) # True if this is a reference file (points to another file)
    
    # declares a foreign key called original_file that connects a file record to another record 
    # in the same table — specifically, the “original” version of a file when deduplication occurs
    # Starts as None with new file, duplicate upload points to original file ID, if orignial deleted, field on reference becomes NULL
    original_file = models.ForeignKey(
        'self', # this foreign key points to the same model (links one file instance to another)
        # allow the field to be empty in both the database (null=True) and forms (blank=True)
        # original files don't have an original_file, only duplicates do
        null=True,
        blank=True,
        # Setting up reverse relationship to access all references to a file (original.references.all())
        related_name='references', # original.references -> all references pointing to it
        on_delete=models.SET_NULL, # if original is deleted, references are kept (we promote one to new original). This makes sure we don't delete the references too
    )

    class Meta:
        # default ordering: show newest uploads first
        ordering = ['-uploaded_at']
        # Indexes optimize the list filters and hash lookups
        # Jump directly to matching rows instead of scanning entire table
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
