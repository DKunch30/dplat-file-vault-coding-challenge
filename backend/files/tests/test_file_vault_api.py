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
    - Upload + deduplication (original/reference)
    - Filters (search/type/size/date)
    - File types and storage stats
    - Throttling (2 req/sec)
    - Quota (10 MB per user)
    - Delete → promotion → physical cleanup
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

    def test_05_quota_enforcement_per_user(self):
        six_mb = make_file_bytes(6 * 1024 * 1024, seed=31)
        five_mb = make_file_bytes(5 * 1024 * 1024, seed=32)

        ok = self._upload_bytes(six_mb, "six.bin", self.h_u2)
        self.assertEqual(ok.status_code, 201)

        over = self._upload_bytes(five_mb, "five.bin", self.h_u2)
        self.assertEqual(over.status_code, 429)
        self.assertIn("Storage Quota Exceeded", over.json().get("detail", ""))

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
