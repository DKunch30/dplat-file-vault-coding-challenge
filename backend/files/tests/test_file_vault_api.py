import os
import time
import shutil
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone

from django.test import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.db import connection
from rest_framework.test import APITestCase

from files.models import File
from files.views import FileViewSet
from files.throttling import UserIdRateThrottle


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_file_bytes(size_bytes: int, seed: int = 123) -> bytes:
    # deterministic bytes for stable hashes
    return (seed.to_bytes(4, "big") * ((size_bytes // 4) + 1))[:size_bytes]

class TestFileVaultAPI(APITestCase):
    """
    End-to-end tests for File Vault API:
        - Upload + deduplication (original/reference) for both same user and different users
        - Filters (search/type/size/date)
        - File types and storage stats
        - Throttling (2 req/sec)
        - Quota (10 MB per user)
        - Delete → promotion → physical cleanup
    
    Each test isolates MEDIA_ROOT via a temp dir and cleans up afterward.
    We keep throttling disabled except in the specific throttling test to avoid flakiness.
    make_file_bytes() is deterministic → stable hashes for reproducible tests.
    """

    def setUp(self):
        # fresh MEDIA_ROOT per test
        self.temp_media_dir = tempfile.mkdtemp(prefix="media_")
        self.addCleanup(lambda: shutil.rmtree(self.temp_media_dir, ignore_errors=True))

        # disable throttling globally during most tests
        self.base_overrides = override_settings(
            MEDIA_ROOT=self.temp_media_dir,
            FILE_VAULT={"STORAGE_QUOTA_MB": 10, "USERID_THROTTLE_RATE": "2/second"},
            REST_FRAMEWORK={
                "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
                "DEFAULT_PARSER_CLASSES": [
                    "rest_framework.parsers.JSONParser",
                    "rest_framework.parsers.MultiPartParser",
                    "rest_framework.parsers.FormParser",
                ],
                "DEFAULT_THROTTLE_CLASSES": [],
                "DEFAULT_THROTTLE_RATES": {"userid": "2/second"},
                "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
                "PAGE_SIZE": 20,
            },
        )
        self.base_overrides.enable()
        self.addCleanup(self.base_overrides.disable)

        # patch view throttles so no 429s
        self._orig_view_throttles = FileViewSet.throttle_classes
        FileViewSet.throttle_classes = []
        self.addCleanup(self._restore_view_throttles)

        cache.clear()
        self.base = "/api/files/"
        self.h_u1 = {"HTTP_USERID": "u1"}
        self.h_u2 = {"HTTP_USERID": "u2"}

    def _restore_view_throttles(self):
        FileViewSet.throttle_classes = self._orig_view_throttles

    # ------------------ helpers ------------------
    def _upload_bytes(self, b: bytes, name: str, headers: dict):
        # infer MIME type based on file extension
        ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "")
        mime_map = {
            "pdf": "application/pdf",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "txt": "text/plain",
            "bin": "application/octet-stream",
        }
        content_type = mime_map.get(ext, "application/octet-stream")
        f = SimpleUploadedFile(name, b, content_type=content_type)
        return self.client.post(self.base, {"file": f}, format="multipart", **headers)

    def _list(self, headers: dict, params: dict = None):
        return self.client.get(self.base, data=(params or {}), **headers)

    def _retrieve(self, file_id: str, headers: dict):
        return self.client.get(f"{self.base}{file_id}/", **headers)

    def _delete(self, file_id: str, headers: dict):
        return self.client.delete(f"{self.base}{file_id}/", **headers)

    def _file_types(self, headers: dict):
        return self.client.get(f"{self.base}file_types/", **headers)

    def _storage_stats(self, headers: dict):
        return self.client.get(f"{self.base}storage_stats/", **headers)

    # ------------------ tests ------------------
    '''
        Upload + dedup same user
            First upload → original
            Second upload (same bytes) → reference with original_file set
            Asserts is_reference and hash equality.
    '''
    def test_01_upload_and_dedup_same_user(self):
        content = make_file_bytes(102_400, seed=7)
        h = sha256_bytes(content)

        r1 = self._upload_bytes(content, "doc.pdf", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        d1 = r1.json()
        self.assertFalse(d1["is_reference"])
        self.assertIsNone(d1["original_file"])
        self.assertEqual(d1["file_hash"], h)

        r2 = self._upload_bytes(content, "doc.pdf", self.h_u1)
        self.assertEqual(r2.status_code, 201)
        d2 = r2.json()
        self.assertTrue(d2["is_reference"])
        self.assertEqual(d2["original_file"], d1["id"])
        self.assertEqual(d2["file_hash"], h)

        r_get = self._retrieve(d1["id"], self.h_u1)
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["reference_count"], 1)

    '''
        Upload + dedup different user
            First upload → original
            Second upload (same bytes) → reference with original_file set
            Asserts is_reference and hash equality.
    '''
    def test_01b_upload_and_dedup_different_user(self):
        # Same content uploaded by two different users:
        # u1 should get the original; u2 should get a reference to u1's original.
        content = make_file_bytes(102_400, seed=7)
        h = sha256_bytes(content)

        # u1 upload -> original
        r1 = self._upload_bytes(content, "doc.pdf", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        d1 = r1.json()
        self.assertFalse(d1["is_reference"])
        self.assertIsNone(d1["original_file"])
        self.assertEqual(d1["file_hash"], h)

        # (small pause to avoid throttle noise)
        time.sleep(0.2)

        # u2 upload same bytes -> reference to u1's original
        r2 = self._upload_bytes(content, "doc.pdf", self.h_u2)
        self.assertEqual(r2.status_code, 201)
        d2 = r2.json()
        self.assertTrue(d2["is_reference"])
        self.assertEqual(d2["original_file"], d1["id"])
        self.assertEqual(d2["file_hash"], h)

        # Fetch u1's original; reference_count should reflect u2's reference (excludes original)
        r_get = self._retrieve(d1["id"], self.h_u1)
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["reference_count"], 1)

        # (Optional) Prove per-user storage accounting:
        # u1 has 1 unique hash -> total_storage_used = size
        # u2 also has 1 unique hash (even though it's a reference) -> total_storage_used = size
        st1 = self.client.get(f"{self.base}storage_stats/", **self.h_u1)
        st2 = self.client.get(f"{self.base}storage_stats/", **self.h_u2)
        self.assertEqual(st1.status_code, 200)
        self.assertEqual(st2.status_code, 200)
        self.assertEqual(st1.json()["total_storage_used"], len(content))
        self.assertEqual(st2.json()["total_storage_used"], len(content))


    '''
        Upload different File types and storage stats:
            Upload PDF and PNG
            Call /file_types/ and /storage_stats/
            Asserts file types and storage stats are reasonable values.
    '''
    def test_02_file_types_and_storage_stats(self):
        pdf = make_file_bytes(50_000, seed=11)
        png = make_file_bytes(60_000, seed=12)

        r1 = self._upload_bytes(pdf, "a.pdf", self.h_u1)
        r2 = self._upload_bytes(png, "b.png", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)

        # duplicate to create a reference
        r3 = self._upload_bytes(pdf, "a2.pdf", self.h_u1)
        self.assertEqual(r3.status_code, 201)
        self.assertTrue(r3.json()["is_reference"])

        rt = self._file_types(self.h_u1)
        self.assertEqual(rt.status_code, 200)
        types = set(rt.json())
        self.assertTrue(any("pdf" in t for t in types))

        rs = self._storage_stats(self.h_u1).json()
        self.assertGreater(rs["original_storage_used"], rs["total_storage_used"])
        self.assertGreater(rs["storage_savings"], 0)

    '''
        Filters, search, size, date:
            Upload small and big files
            search, min_size, and start_date/end_date (ISO 8601)
            Call /list/ with filters
            Asserts results match constraints.
    '''
    def test_03_filters_search_size_date(self):
        content_small = make_file_bytes(10_000, seed=21)
        content_big = make_file_bytes(200_000, seed=22)

        r1 = self._upload_bytes(content_small, "report-final.pdf", self.h_u1)
        r2 = self._upload_bytes(content_big, "notes.txt", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)

        q = self._list(self.h_u1, {"search": "report-final"})
        self.assertEqual(q.status_code, 200)
        names = [row["original_filename"] for row in q.json()["results"]]
        self.assertIn("report-final.pdf", names)

        q2 = self._list(self.h_u1, {"min_size": 150_000})
        sizes = [row["size"] for row in q2.json()["results"]]
        self.assertTrue(all(s >= 150_000 for s in sizes))

        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        q3 = self._list(self.h_u1, {"start_date": start.isoformat(), "end_date": end.isoformat()})
        self.assertEqual(q3.status_code, 200)
        self.assertGreaterEqual(q3.json()["count"], 2)

    '''
        Throttling (userid):
            Re-enable throttling (UserIdRateThrottle) only here
            asserts 3rd call within a second returns 429 then recovers after 1s
    '''
    def test_04_throttling_userid(self):
        # re-enable throttling only here
        FileViewSet.throttle_classes = [UserIdRateThrottle]
        cache.clear()

        with override_settings(
            FILE_VAULT={"STORAGE_QUOTA_MB": 10, "USERID_THROTTLE_RATE": "2/second"},
            REST_FRAMEWORK={
                "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
                "DEFAULT_PARSER_CLASSES": [
                    "rest_framework.parsers.JSONParser",
                    "rest_framework.parsers.MultiPartParser",
                    "rest_framework.parsers.FormParser",
                ],
                "DEFAULT_THROTTLE_CLASSES": ["files.throttling.UserIdRateThrottle"],
                "DEFAULT_THROTTLE_RATES": {"userid": "2/second"},
            },
        ):
            r1 = self._list(self.h_u1)
            r2 = self._list(self.h_u1)
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r2.status_code, 200)

            r3 = self._list(self.h_u1)
            self.assertEqual(r3.status_code, 429)
            self.assertIn("Call Limit Reached", r3.json().get("detail", ""))

            time.sleep(1.1)
            r4 = self._list(self.h_u1)
            self.assertEqual(r4.status_code, 200)

        FileViewSet.throttle_classes = []

    '''
        Quota enforcement (per-user):
            With 10 MB quota,
            Upload 6 MB file.
            Asserts success ok.
            Upload 5 MB file
            Asserts quota exceeded 429 (“Storage Quota Exceeded”).
            This validates dedup-aware quota math.
    '''
    def test_05_quota_enforcement_per_user(self):
        six_mb = make_file_bytes(6 * 1024 * 1024, seed=31)
        five_mb = make_file_bytes(5 * 1024 * 1024, seed=32)

        ok = self._upload_bytes(six_mb, "six.bin", self.h_u2)
        self.assertEqual(ok.status_code, 201)

        over = self._upload_bytes(five_mb, "five.bin", self.h_u2)
        self.assertEqual(over.status_code, 429)
        self.assertIn("Storage Quota Exceeded", over.json().get("detail", ""))

    '''
        Delete, promotion, and physical cleanup:
            Upload same file twice
            Asserts original and reference are reasonable.
            Delete original
            Asserts reference is promoted to original and physical cleanup.
            Delete the promoted one; 
            Verifies the physical file is removed when no DB rows remain for that hash.
    '''
    def test_06_delete_promotion_and_physical_cleanup(self):
        data = make_file_bytes(80_000, seed=41)

        r1 = self._upload_bytes(data, "x.pdf", self.h_u1)
        r2 = self._upload_bytes(data, "x2.pdf", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)

        d1 = r1.json()
        d2 = r2.json()
        self.assertFalse(d1["is_reference"])
        self.assertTrue(d2["is_reference"])
        self.assertEqual(d2["original_file"], d1["id"])

        obj_original = File.objects.get(id=d1["id"])
        file_path = obj_original.file.path
        self.assertTrue(os.path.exists(file_path))

        del1 = self._delete(d1["id"], self.h_u1)
        self.assertEqual(del1.status_code, 204)

        promoted = File.objects.get(id=d2["id"])
        self.assertFalse(promoted.is_reference)
        self.assertIsNone(promoted.original_file)

        del2 = self._delete(promoted.id, self.h_u1)
        self.assertEqual(del2.status_code, 204)

        self.assertFalse(File.objects.filter(file_hash=d1["file_hash"]).exists())
        self.assertFalse(os.path.exists(file_path), "Physical file should be deleted when no rows remain")

    def test_07_reference_count_detailed_scenarios(self):
        """
        Detailed test for reference_count behavior with the new semantics:
        reference_count now includes the original file in the count.
        
        Test scenarios:
        1. Single original file → reference_count = 1
        2. Original + 1 reference → reference_count = 2 for both
        3. Original + 2 references → reference_count = 3 for all
        4. Cross-user deduplication → reference_count reflects global dedup group size
        5. After deletion and promotion → reference_count updates correctly
        """
        # Create deterministic content for testing
        content_a = make_file_bytes(50_000, seed=100)
        content_b = make_file_bytes(60_000, seed=200)
        
        # === Scenario 1: Single original file ===
        # Input: Upload one unique file
        # Expected: reference_count = 1 (just the original itself)
        r1 = self._upload_bytes(content_a, "single.pdf", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        d1 = r1.json()
        
        # Verify it's an original (not a reference)
        self.assertFalse(d1["is_reference"])
        self.assertIsNone(d1["original_file"])
        
        # NEW BEHAVIOR: reference_count includes the original itself
        self.assertEqual(d1["reference_count"], 1, 
                        "Single original should have reference_count=1 (includes itself)")
        
        # === Scenario 2: Original + 1 reference (same user) ===
        # Input: Upload the same content again by the same user
        # Expected: reference_count = 2 for both original and reference
        r2 = self._upload_bytes(content_a, "duplicate.pdf", self.h_u1)
        self.assertEqual(r2.status_code, 201)
        d2 = r2.json()
        
        # Verify it's a reference pointing to the original
        self.assertTrue(d2["is_reference"])
        self.assertEqual(d2["original_file"], d1["id"])
        self.assertEqual(d2["file_hash"], d1["file_hash"])
        
        # Both should now report reference_count = 2
        self.assertEqual(d2["reference_count"], 2,
                        "Reference should report same count as original: 2 (original + 1 reference)")
        
        # Re-fetch the original to verify its count updated
        r1_updated = self._retrieve(d1["id"], self.h_u1)
        self.assertEqual(r1_updated.status_code, 200)
        self.assertEqual(r1_updated.json()["reference_count"], 2,
                        "Original should now report reference_count=2 after adding one reference")
        
        # === Scenario 3: Original + 2 references (add another reference) ===
        # Input: Upload the same content a third time
        # Expected: reference_count = 3 for all files in the dedup group
        r3 = self._upload_bytes(content_a, "triple.pdf", self.h_u1)
        self.assertEqual(r3.status_code, 201)
        d3 = r3.json()
        
        # Verify it's also a reference to the same original
        self.assertTrue(d3["is_reference"])
        self.assertEqual(d3["original_file"], d1["id"])
        
        # All three should report reference_count = 3
        self.assertEqual(d3["reference_count"], 3,
                        "Third reference should report count=3 (original + 2 references)")
        
        # Verify original and first reference also updated to 3
        r1_final = self._retrieve(d1["id"], self.h_u1)
        r2_updated = self._retrieve(d2["id"], self.h_u1)
        self.assertEqual(r1_final.json()["reference_count"], 3)
        self.assertEqual(r2_updated.json()["reference_count"], 3)
        
        # === Scenario 4: Cross-user deduplication ===
        # Input: Different user uploads the same content
        # Expected: reference_count = 4 (reflects global dedup group size)
        time.sleep(0.1)  # Small delay to avoid potential throttling
        r4 = self._upload_bytes(content_a, "user2_copy.pdf", self.h_u2)
        self.assertEqual(r4.status_code, 201)
        d4 = r4.json()
        
        # Should be a reference to u1's original
        self.assertTrue(d4["is_reference"])
        self.assertEqual(d4["original_file"], d1["id"])
        
        # All files in the dedup group should now report count = 4
        self.assertEqual(d4["reference_count"], 4,
                        "Cross-user reference should report global dedup group size: 4")
        
        # Verify the original also reflects the new count
        r1_cross_user = self._retrieve(d1["id"], self.h_u1)
        self.assertEqual(r1_cross_user.json()["reference_count"], 4,
                        "Original should reflect cross-user references in count")

    def test_08_reference_count_after_deletion_and_promotion(self):
        """
        Test reference_count behavior during deletion and promotion scenarios.
        
        Scenarios:
        1. Delete a reference → reference_count decreases for remaining files
        2. Delete original → promotion occurs, reference_count adjusts
        3. Delete all but one → reference_count = 1 for the survivor
        """
        content = make_file_bytes(40_000, seed=300)
        
        # Create original + 2 references (total count should be 3)
        r1 = self._upload_bytes(content, "original.txt", self.h_u1)  # original
        r2 = self._upload_bytes(content, "ref1.txt", self.h_u1)      # reference 1
        r3 = self._upload_bytes(content, "ref2.txt", self.h_u1)      # reference 2
        
        d1, d2, d3 = r1.json(), r2.json(), r3.json()
        
        # Verify initial state: all should have reference_count = 3
        self.assertEqual(d1["reference_count"], 3)
        self.assertEqual(d2["reference_count"], 3)
        self.assertEqual(d3["reference_count"], 3)
        
        # === Scenario 1: Delete a reference ===
        # Input: Delete one of the references (not the original)
        # Expected: reference_count decreases to 2 for remaining files
        del_ref1 = self._delete(d2["id"], self.h_u1)
        self.assertEqual(del_ref1.status_code, 204)
        
        # Check remaining files: original and ref2 should now show count = 2
        r1_after_del = self._retrieve(d1["id"], self.h_u1)
        r3_after_del = self._retrieve(d3["id"], self.h_u1)
        
        self.assertEqual(r1_after_del.json()["reference_count"], 2,
                        "After deleting one reference, count should decrease to 2")
        self.assertEqual(r3_after_del.json()["reference_count"], 2,
                        "Remaining reference should also show updated count")
        
        # === Scenario 2: Delete the original (triggers promotion) ===
        # Input: Delete the original file
        # Expected: ref2 gets promoted to original, reference_count = 1
        del_original = self._delete(d1["id"], self.h_u1)
        self.assertEqual(del_original.status_code, 204)
        
        # Fetch the remaining file (d3/ref2) - it should now be promoted to original
        r3_promoted = self._retrieve(d3["id"], self.h_u1)
        self.assertEqual(r3_promoted.status_code, 200)
        promoted_data = r3_promoted.json()
        
        # Verify promotion occurred
        self.assertFalse(promoted_data["is_reference"], 
                        "After original deletion, reference should be promoted to original")
        self.assertIsNone(promoted_data["original_file"],
                         "Promoted file should have original_file = null")
        
        # reference_count should now be 1 (only this file remains)
        self.assertEqual(promoted_data["reference_count"], 1,
                        "After promotion with no other references, count should be 1")

    def test_09_edge_cases_and_error_scenarios(self):
        """
        Test edge cases and error scenarios with detailed expected behaviors.
        
        Scenarios:
        1. Upload without UserId header → 403 Forbidden
        2. Upload empty file → should work, reference_count = 1
        3. Upload file exceeding quota → 429 Too Many Requests
        4. Retrieve non-existent file → 404 Not Found
        5. Delete non-existent file → 404 Not Found
        6. Cross-user file access → 404 (user isolation)
        """
        
        # === Scenario 1: Missing UserId header ===
        # Input: POST request without UserId header
        # Expected: 403 Forbidden with specific error message
        content = make_file_bytes(1000, seed=400)
        f = SimpleUploadedFile("test.txt", content, content_type="text/plain")
        
        response_no_header = self.client.post(self.base, {"file": f}, format="multipart")
        self.assertEqual(response_no_header.status_code, 403,
                        "Request without UserId header should return 403")
        self.assertIn("Missing required UserId header", 
                     response_no_header.json().get("detail", ""),
                     "Error message should indicate missing UserId header")
        
        # === Scenario 2: Upload empty file ===
        # Input: Upload a 0-byte file
        # Expected: Success with reference_count = 1
        empty_content = b""
        r_empty = self._upload_bytes(empty_content, "empty.txt", self.h_u1)
        self.assertEqual(r_empty.status_code, 201, "Empty file upload should succeed")
        
        d_empty = r_empty.json()
        self.assertEqual(d_empty["size"], 0, "Empty file should have size = 0")
        self.assertEqual(d_empty["reference_count"], 1, 
                        "Empty file should have reference_count = 1")
        self.assertFalse(d_empty["is_reference"], "First upload should be original")
        
        # Upload another empty file - should create reference
        r_empty2 = self._upload_bytes(empty_content, "empty2.txt", self.h_u1)
        self.assertEqual(r_empty2.status_code, 201)
        d_empty2 = r_empty2.json() 
        self.assertTrue(d_empty2["is_reference"], "Second empty file should be reference")
        self.assertEqual(d_empty2["reference_count"], 2, 
                        "Both empty files should show reference_count = 2")
        
        # === Scenario 3: Quota exceeded ===
        # Input: Upload files that exceed the 10MB quota
        # Expected: First large file succeeds, second fails with 429
        large_file_7mb = make_file_bytes(7 * 1024 * 1024, seed=500)  # 7MB
        large_file_4mb = make_file_bytes(4 * 1024 * 1024, seed=600)  # 4MB
        
        # First upload should succeed (7MB < 10MB quota)
        r_large1 = self._upload_bytes(large_file_7mb, "large1.bin", self.h_u2)
        self.assertEqual(r_large1.status_code, 201, "7MB file should fit in 10MB quota")
        
        # Second upload should fail (7MB + 4MB = 11MB > 10MB quota)
        r_large2 = self._upload_bytes(large_file_4mb, "large2.bin", self.h_u2)
        self.assertEqual(r_large2.status_code, 429, "Should exceed quota and return 429")
        self.assertIn("Storage Quota Exceeded", 
                     r_large2.json().get("detail", ""),
                     "Error should indicate quota exceeded")
        
        # === Scenario 4: Retrieve non-existent file ===
        # Input: GET request for a UUID that doesn't exist
        # Expected: 404 Not Found
        fake_uuid = "00000000-0000-0000-0000-000000000000"
        r_fake = self._retrieve(fake_uuid, self.h_u1)
        self.assertEqual(r_fake.status_code, 404, 
                        "Non-existent file should return 404")
        
        # === Scenario 5: Delete non-existent file ===
        # Input: DELETE request for a UUID that doesn't exist
        # Expected: 404 Not Found
        del_fake = self._delete(fake_uuid, self.h_u1)
        self.assertEqual(del_fake.status_code, 404,
                        "Deleting non-existent file should return 404")
        
        # === Scenario 6: Cross-user file access ===
        # Input: User u1 tries to access user u2's file
        # Expected: 404 (user isolation - file exists but not accessible)
        u2_content = make_file_bytes(2000, seed=700)
        r_u2 = self._upload_bytes(u2_content, "u2_file.txt", self.h_u2)
        self.assertEqual(r_u2.status_code, 201)
        u2_file_id = r_u2.json()["id"]
        
        # u1 tries to access u2's file
        r_cross_access = self._retrieve(u2_file_id, self.h_u1)
        self.assertEqual(r_cross_access.status_code, 404,
                        "User should not be able to access another user's file")
        
        # u1 tries to delete u2's file
        del_cross_access = self._delete(u2_file_id, self.h_u1)
        self.assertEqual(del_cross_access.status_code, 404,
                        "User should not be able to delete another user's file")

    def test_10_storage_stats_with_reference_counting(self):
        """
        Test storage_stats endpoint with detailed scenarios and reference counting.
        
        Scenarios:
        1. No files → all stats should be 0
        2. Single file → original_storage_used = total_storage_used, no savings
        3. Duplicates → original_storage_used > total_storage_used, savings > 0
        4. Mixed file sizes → verify deduplication math
        5. Cross-user impact → each user's stats are isolated
        """
        
        # === Scenario 1: No files uploaded ===
        # Input: Call storage_stats with no files
        # Expected: All values should be 0
        stats_empty = self._storage_stats(self.h_u1)
        self.assertEqual(stats_empty.status_code, 200)
        empty_data = stats_empty.json()
        
        expected_empty = {
            "user_id": "u1",
            "total_storage_used": 0,
            "original_storage_used": 0,
            "storage_savings": 0,
            "savings_percentage": 0.0
        }
        self.assertEqual(empty_data, expected_empty,
                        "Empty storage stats should show all zeros")
        
        # === Scenario 2: Single unique file ===
        # Input: Upload one file
        # Expected: original_storage_used = total_storage_used, no savings
        file_5kb = make_file_bytes(5000, seed=800)
        r1 = self._upload_bytes(file_5kb, "unique.txt", self.h_u1)
        self.assertEqual(r1.status_code, 201)
        
        stats_single = self._storage_stats(self.h_u1)
        single_data = stats_single.json()
        
        expected_single = {
            "user_id": "u1",
            "total_storage_used": 5000,      # Only one unique file
            "original_storage_used": 5000,   # Only one upload
            "storage_savings": 0,            # No duplicates = no savings
            "savings_percentage": 0.0
        }
        self.assertEqual(single_data, expected_single,
                        "Single file should show no deduplication savings")
        
        # === Scenario 3: Upload duplicate (same content) ===
        # Input: Upload the same content again
        # Expected: original_storage_used doubles, total_storage_used stays same
        r2 = self._upload_bytes(file_5kb, "duplicate.txt", self.h_u1)
        self.assertEqual(r2.status_code, 201)
        
        # Verify it's a reference
        self.assertTrue(r2.json()["is_reference"])
        
        stats_dup = self._storage_stats(self.h_u1)
        dup_data = stats_dup.json()
        
        expected_dup = {
            "user_id": "u1",
            "total_storage_used": 5000,      # Still only one unique file
            "original_storage_used": 10000,  # Two uploads of 5KB each
            "storage_savings": 5000,         # 10000 - 5000 = 5000 bytes saved
            "savings_percentage": 50.0       # 5000/10000 * 100 = 50%
        }
        self.assertEqual(dup_data, expected_dup,
                        "Duplicate should show 50% storage savings")
        
        # === Scenario 4: Mixed file sizes with partial deduplication ===
        # Input: Add a different file, then duplicate it
        # Expected: Complex deduplication math
        file_3kb = make_file_bytes(3000, seed=900)
        r3 = self._upload_bytes(file_3kb, "different.txt", self.h_u1)  # New unique file
        r4 = self._upload_bytes(file_3kb, "different_dup.txt", self.h_u1)  # Duplicate of 3KB
        
        self.assertEqual(r3.status_code, 201)
        self.assertEqual(r4.status_code, 201)
        self.assertFalse(r3.json()["is_reference"])  # Original
        self.assertTrue(r4.json()["is_reference"])   # Reference
        
        stats_mixed = self._storage_stats(self.h_u1)
        mixed_data = stats_mixed.json()
        
        # Math: 
        # - original_storage_used = 5000 + 5000 + 3000 + 3000 = 16000
        # - total_storage_used = 5000 + 3000 = 8000 (two unique files)
        # - savings = 16000 - 8000 = 8000
        # - percentage = 8000/16000 * 100 = 50%
        expected_mixed = {
            "user_id": "u1",
            "total_storage_used": 8000,
            "original_storage_used": 16000,
            "storage_savings": 8000,
            "savings_percentage": 50.0
        }
        self.assertEqual(mixed_data, expected_mixed,
                        "Mixed files should show correct deduplication math")
        
        # === Scenario 5: Cross-user isolation ===
        # Input: Different user uploads files
        # Expected: Each user's stats are completely separate
        u2_file = make_file_bytes(2000, seed=1000)
        r_u2 = self._upload_bytes(u2_file, "u2_file.txt", self.h_u2)
        self.assertEqual(r_u2.status_code, 201)
        
        # u1's stats should be unchanged
        stats_u1_after = self._storage_stats(self.h_u1)
        self.assertEqual(stats_u1_after.json(), expected_mixed,
                        "u1's stats should not be affected by u2's uploads")
        
        # u2's stats should only reflect their upload
        stats_u2 = self._storage_stats(self.h_u2)
        u2_data = stats_u2.json()
        
        expected_u2 = {
            "user_id": "u2",
            "total_storage_used": 2000,
            "original_storage_used": 2000,
            "storage_savings": 0,
            "savings_percentage": 0.0
        }
        self.assertEqual(u2_data, expected_u2,
                        "u2's stats should be isolated from u1's")
