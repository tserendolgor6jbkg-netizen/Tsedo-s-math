import base64
import io
import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from xml.etree import ElementTree as ET
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlencode

import server


class APIClient:
    def __init__(self, base_url):
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Origin": self.base_url} if data is not None else {},
        )
        try:
            response = self.opener.open(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read().decode("utf-8"))

    def request_bytes(self, method, path, data, content_type="application/octet-stream"):
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": content_type,
                "Origin": self.base_url,
            },
        )
        try:
            response = self.opener.open(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read().decode("utf-8"))


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = server.DB_PATH
        self.previous_profile_photo_dir = server.PROFILE_PHOTO_DIR
        self.previous_cover_image_dir = server.COVER_IMAGE_DIR
        self.previous_lesson_media_dir = server.LESSON_MEDIA_DIR
        server.DB_PATH = Path(self.temp_dir.name) / "platform.sqlite3"
        server.PROFILE_PHOTO_DIR = Path(self.temp_dir.name) / "profiles"
        server.COVER_IMAGE_DIR = Path(self.temp_dir.name) / "cover"
        server.LESSON_MEDIA_DIR = Path(self.temp_dir.name) / "lessons"
        with server.RATE_LOCK:
            server.RATE_LIMITS.clear()
        server.schema()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.teacher_invite_code = "TESTTEACHER42"
        self.httpd.dummy_password_hash = server.password_hash("dummy timing password")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.client = APIClient(self.base_url)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        server.DB_PATH = self.previous_db_path
        server.PROFILE_PHOTO_DIR = self.previous_profile_photo_dir
        server.COVER_IMAGE_DIR = self.previous_cover_image_dir
        server.LESSON_MEDIA_DIR = self.previous_lesson_media_dir
        self.temp_dir.cleanup()

    def post(self, client, path, payload):
        return client.request("POST", path, payload)

    def test_seeded_course_and_question_integrity(self):
        course = server.load_curriculum()
        self.assertEqual(len(course["chapters"]), 7)
        self.assertEqual(sum(len(chapter["topics"]) for chapter in course["chapters"]), 37)
        with server.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM criteria").fetchone()[0], 14)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM objectives").fetchone()[0], 39)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM objectives WHERE starred=1").fetchone()[0], 14)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0], 120)
            for level in sorted(server.LEVELS):
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM exercises WHERE level=?", (level,)).fetchone()[0],
                    40,
                )
            rows = db.execute("SELECT answer_key FROM exercises").fetchall()
            exercise_records = db.execute(
                "SELECT chapter_id,topic,prompt,answer_key,criteria_json FROM exercises ORDER BY id"
            ).fetchall()
            valid_criteria = {
                chapter["id"]: {
                    row["id"]
                    for row in db.execute("SELECT id FROM criteria WHERE chapter_id=?", (chapter["id"],))
                }
                for chapter in course["chapters"]
            }
        for row in rows:
            self.assertTrue(server.answer_matches(row["answer_key"], row["answer_key"]))
        prompts = [row["prompt"] for row in exercise_records]
        self.assertEqual(len(prompts), len(set(prompts)))
        for row in exercise_records:
            self.assertTrue(server.answer_matches(row["answer_key"], row["answer_key"]))
            self.assertTrue(set(json.loads(row["criteria_json"])).issubset(valid_criteria[row["chapter_id"]]))
        for chapter in course["chapters"]:
            chapter_topics = {row["topic"] for row in exercise_records if row["chapter_id"] == chapter["id"]}
            self.assertEqual(chapter_topics, set(chapter["topics"]))
        with server.connect_db() as db:
            for level in sorted(server.LEVELS):
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM exercises WHERE chapter_id='polynomials' AND level=?", (level,)).fetchone()[0],
                    4,
                )
            for chapter_id in ("functions", "derivatives", "integrals", "probability", "complex"):
                for level in sorted(server.LEVELS):
                    self.assertEqual(
                        db.execute(
                            "SELECT COUNT(*) FROM exercises WHERE chapter_id=? AND level=?",
                            (chapter_id, level),
                        ).fetchone()[0],
                        6 if chapter_id == "functions" else 5,
                    )
        self.assertTrue(server.answer_matches("-6, 2", "2,-6"))
        self.assertTrue(server.answer_matches("3x²−2x+3", "3x^2-2x+3"))
        self.assertTrue(server.answer_matches("x²−2x−8", "x^2-2x-8"))
        self.assertFalse(server.answer_matches("8", "7"))

    def test_curriculum_learning_content_and_quiz_integrity(self):
        self.assertEqual(server.load_eesh_quizzes(), [])
        lessons = server.load_lessons()
        self.assertEqual(len(lessons), 37)
        polynomial_lessons = [lesson for lesson in lessons if lesson["chapter_id"] == "polynomials"]
        function_lessons = [lesson for lesson in lessons if lesson["chapter_id"] == "functions"]
        derivative_lessons = [lesson for lesson in lessons if lesson["chapter_id"] == "derivatives"]
        integral_lessons = [lesson for lesson in lessons if lesson["chapter_id"] == "integrals"]
        probability_lessons = [lesson for lesson in lessons if lesson["chapter_id"] == "probability"]
        complex_lessons = [lesson for lesson in lessons if lesson["chapter_id"] == "complex"]
        self.assertEqual(
            [lesson["topic"] for lesson in polynomial_lessons],
            server.load_curriculum()["chapters"][1]["topics"],
        )
        self.assertEqual(len(polynomial_lessons), 4)
        curriculum_chapters = server.load_curriculum()["chapters"]
        self.assertEqual(
            [lesson["topic"] for lesson in function_lessons],
            curriculum_chapters[2]["topics"],
        )
        self.assertEqual(
            [lesson["topic"] for lesson in derivative_lessons],
            curriculum_chapters[3]["topics"],
        )
        self.assertEqual(
            {code for lesson in function_lessons for code in lesson["objective_codes"]},
            {"12.3а", "12.3б", "12.3в", "12.3г", "12.3д"},
        )
        self.assertEqual(
            {code for lesson in derivative_lessons for code in lesson["objective_codes"]},
            {"12.7а", "12.7б", "12.7в", "12.7г*"},
        )
        self.assertEqual(
            [lesson["topic"] for lesson in integral_lessons],
            curriculum_chapters[4]["topics"],
        )
        self.assertEqual(
            {code for lesson in integral_lessons for code in lesson["objective_codes"]},
            {"12.8а", "12.8б", "12.8в", "12.8г", "12.8д*", "12.8е*", "12.8ж*"},
        )
        self.assertEqual(
            [lesson["topic"] for lesson in probability_lessons],
            curriculum_chapters[5]["topics"],
        )
        self.assertEqual(
            {code for lesson in probability_lessons for code in lesson["objective_codes"]},
            {"12.12а", "12.12б", "12.12в*", "12.12г*", "13а*", "13б*", "13в*"},
        )
        self.assertEqual(
            [lesson["topic"] for lesson in complex_lessons],
            curriculum_chapters[6]["topics"],
        )
        self.assertEqual(
            {code for lesson in complex_lessons for code in lesson["objective_codes"]},
            {"12.4а", "12.4б", "12.4в", "12.4г", "12.4д", "12.4е*", "12.4ж*", "12.4з*", "12.4и*"},
        )
        self.assertEqual(
            {code for lesson in polynomial_lessons for code in lesson["objective_codes"]},
            {"12.2а", "12.2б", "12.2в", "12.2г", "12.2д", "12.2е*"},
        )
        quiz_ids = []
        for lesson in lessons:
            self.assertIn(
                lesson["chapter_id"],
                {"absolute", "polynomials", "functions", "derivatives", "integrals", "probability", "complex"},
            )
            self.assertTrue(lesson["objective_codes"])
            self.assertTrue(lesson["criteria"])
            self.assertTrue(lesson["worked_examples"])
            self.assertTrue(lesson["formulas"])
            self.assertTrue(lesson["glossary"])
            self.assertTrue(lesson["recap"])
            self.assertTrue(lesson["connections"])
            self.assertGreaterEqual(len(lesson["quiz"]), 2)
            for question in lesson["quiz"]:
                quiz_ids.append(question["id"])
                self.assertTrue(question["explanation"])
                self.assertTrue(server.answer_matches(question["answer"], question["answer"]))
            for connection in lesson["connections"]:
                self.assertIn(connection["lesson_id"], {item["id"] for item in lessons})
        self.assertEqual(len(quiz_ids), len(set(quiz_ids)))

        status, catalog = self.client.request("GET", "/api/learning/lessons")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["chapters"]), 7)
        lesson_counts = {chapter["id"]: chapter["lesson_count"] for chapter in catalog["chapters"]}
        self.assertEqual(lesson_counts["absolute"], 3)
        self.assertEqual(lesson_counts["polynomials"], 4)
        self.assertEqual(lesson_counts["functions"], 7)
        self.assertEqual(lesson_counts["derivatives"], 7)
        self.assertEqual(lesson_counts["integrals"], 5)
        self.assertEqual(lesson_counts["probability"], 3)
        self.assertEqual(lesson_counts["complex"], 8)
        self.assertTrue(
            all(
                count == 0
                for chapter_id, count in lesson_counts.items()
                if chapter_id
                not in {
                    "absolute",
                    "polynomials",
                    "functions",
                    "derivatives",
                    "integrals",
                    "probability",
                    "complex",
                }
            )
        )
        for lesson in catalog["lessons"]:
            self.assertTrue(all("answer" not in question for question in lesson["quiz"]))
            self.assertEqual(lesson["media"], [])

    def test_teacher_lesson_media_upload_preview_and_delete_permissions(self):
        self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test",
            "display_name": "Багш",
            "password": "SecurePassword34",
            "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test",
            "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        lesson_id = "absolute-modulus"

        filename = "lesson.pdf"
        upload_path = f"/api/learning/lessons/{lesson_id}/media?{urlencode({'title': 'Нэмэлт PDF', 'filename': filename})}"
        pdf = b"%PDF-1.7\nSupplementary lesson\n%%EOF"
        self.assertEqual(student_client.request_bytes("POST", upload_path, pdf)[0], 403)
        self.assertEqual(APIClient(self.base_url).request_bytes("POST", upload_path, pdf)[0], 401)
        invalid_status, _ = self.client.request_bytes("POST", upload_path, b"not a pdf")
        self.assertEqual(invalid_status, 400)
        invalid_lesson_path = (
            "/api/learning/lessons/not-a-lesson/media?"
            f"{urlencode({'title': 'PDF', 'filename': 'lesson.pdf'})}"
        )
        self.assertEqual(self.client.request_bytes("POST", invalid_lesson_path, pdf)[0], 404)
        unsupported_path = (
            f"/api/learning/lessons/{lesson_id}/media?"
            f"{urlencode({'title': 'Буруу төрөл', 'filename': 'lesson.exe'})}"
        )
        self.assertEqual(self.client.request_bytes("POST", unsupported_path, pdf)[0], 400)

        pdf_status, pdf_result = self.client.request_bytes("POST", upload_path, pdf)
        self.assertEqual(pdf_status, 201)
        pdf_media = pdf_result["media"]
        self.assertEqual(pdf_media["media_type"], "document")
        self.assertTrue(pdf_media["can_delete"])
        with urllib.request.urlopen(self.base_url + pdf_media["url"]) as response:
            self.assertEqual(response.headers.get_content_type(), "application/pdf")
            self.assertEqual(response.read(), pdf)

        presentation = io.BytesIO()
        with zipfile.ZipFile(presentation, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/presentation.xml", "<presentation/>")
        pptx_path = (
            f"/api/learning/lessons/{lesson_id}/media?"
            f"{urlencode({'title': 'Танилцуулга', 'filename': 'lesson.pptx'})}"
        )
        pptx_status, pptx_result = self.client.request_bytes(
            "POST", pptx_path, presentation.getvalue()
        )
        self.assertEqual(pptx_status, 201)
        self.assertEqual(pptx_result["media"]["media_type"], "presentation")

        video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8
        video_path = (
            f"/api/learning/lessons/{lesson_id}/media?"
            f"{urlencode({'title': 'Тайлбар видео', 'filename': 'lesson.mp4'})}"
        )
        video_status, video_result = self.client.request_bytes("POST", video_path, video)
        self.assertEqual(video_status, 201)
        video_media = video_result["media"]
        self.assertEqual(video_media["media_type"], "video")
        range_request = urllib.request.Request(
            self.base_url + video_media["url"], headers={"Range": "bytes=4-11"}
        )
        with urllib.request.urlopen(range_request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers.get("Content-Range"), f"bytes 4-11/{len(video)}")
            self.assertEqual(response.read(), b"ftypisom")

        catalog = student_client.request("GET", "/api/learning/lessons")[1]
        lesson = next(item for item in catalog["lessons"] if item["id"] == lesson_id)
        self.assertEqual(len(lesson["media"]), 3)
        self.assertTrue(all(not item["can_delete"] for item in lesson["media"]))
        self.assertEqual(
            len(self.client.request("GET", f"/api/learning/lessons/{lesson_id}")[1]["lesson"]["media"]),
            3,
        )

        other_teacher = APIClient(self.base_url)
        self.post(other_teacher, "/api/auth/register", {
            "email": "other-teacher@example.test",
            "display_name": "Өөр багш",
            "password": "SecurePassword56",
            "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        delete_path = f"/api/learning/media/{pdf_media['id']}"
        self.assertEqual(other_teacher.request("DELETE", delete_path, {})[0], 403)
        self.assertEqual(self.client.request("DELETE", delete_path, {})[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base_url + pdf_media["url"])
        self.assertEqual(error.exception.code, 404)

    def test_polynomial_lesson_quiz_accepts_polynomial_notation_and_passes(self):
        self.post(self.client, "/api/auth/register", {
            "email": "polynomial-student@example.test", "display_name": "Олон гишүүнт сурагч",
            "password": "SecurePassword12",
        })
        status, result = self.post(
            self.client,
            "/api/learning/quizzes/polynomial-basics/submit",
            {"answers": {
                "poly-basics-quiz-1": "3",
                "poly-basics-quiz-2": "3x²−2x+3",
                "poly-basics-quiz-3": "x^2-2x-8",
                "poly-basics-quiz-4": "4, 3",
            }},
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["points_earned"], result["points_possible"])
        self.assertTrue(result["passed"])

    def test_lesson_views_quiz_errors_and_teacher_learning_report(self):
        self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test", "display_name": "Багш",
            "password": "SecurePassword34", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        _, classroom = self.post(self.client, "/api/classes", {"name": "12А анги"})
        class_path = f"/api/classes/{classroom['class']['id']}/students"
        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        self.assertEqual(self.post(self.client, "/api/learning/lessons/absolute-modulus/view", {})[0], 403)
        self.assertEqual(self.post(self.client, class_path, {"email": "student@example.test"})[0], 201)

        view_status, _ = self.post(student_client, "/api/learning/lessons/absolute-modulus/view", {})
        self.assertEqual(view_status, 200)
        wrong_status, wrong_result = self.post(
            student_client,
            "/api/learning/quizzes/absolute-modulus/submit",
            {"answers": {"modulus-quiz-1": "13", "modulus-quiz-2": "8"}},
        )
        self.assertEqual(wrong_status, 201)
        self.assertEqual(wrong_result["points_earned"], 1)
        self.assertFalse(wrong_result["passed"])
        self.assertFalse(wrong_result["answers"][0]["correct"])

        state_status, state = student_client.request("GET", "/api/student/learning")
        self.assertEqual(state_status, 200)
        progress = next(item for item in state["lessons"] if item["lesson_id"] == "absolute-modulus")
        self.assertIsNotNone(progress["viewed_at"])
        self.assertIsNone(progress["completed_at"])
        self.assertEqual(len(state["errors"]), 1)
        self.assertEqual(state["errors"][0]["expected_answer"], "12")

        _, passed = self.post(
            student_client,
            "/api/learning/quizzes/absolute-modulus/submit",
            {"answers": {"modulus-quiz-1": "12", "modulus-quiz-2": "8"}},
        )
        self.assertTrue(passed["passed"])
        progress = next(
            item for item in student_client.request("GET", "/api/student/learning")[1]["lessons"]
            if item["lesson_id"] == "absolute-modulus"
        )
        self.assertIsNotNone(progress["completed_at"])
        self.assertEqual(progress["quiz_attempts"], 2)

        daily = student_client.request("GET", "/api/learning/quizzes/daily")[1]
        self.assertTrue(all("answer" not in question for question in daily["questions"]))
        question_id = daily["questions"][0]["id"]
        with server.connect_db() as db:
            expected = db.execute("SELECT answer_key FROM exercises WHERE id=?", (question_id,)).fetchone()[0]
        daily_status, daily_result = self.post(
            student_client,
            "/api/learning/quizzes/daily/submit",
            {"answers": {question_id: expected}},
        )
        self.assertEqual(daily_status, 201)
        self.assertTrue(daily_result["answers"][0]["correct"])
        duplicate_status, _ = self.post(
            student_client,
            "/api/learning/quizzes/daily/submit",
            {"answers": {question_id: expected}},
        )
        self.assertEqual(duplicate_status, 409)

        report_status, report = self.client.request("GET", "/api/teacher/learning")
        self.assertEqual(report_status, 200)
        self.assertEqual(len(report["students"]), 1)
        self.assertEqual(report["students"][0]["viewed_lessons"], 1)
        self.assertEqual(report["students"][0]["completed_lessons"], 1)
        self.assertEqual(report["students"][0]["quiz_attempts"], 3)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["display_name"], "Сурагч")

    def test_registration_login_session_and_role_guards(self):
        status, student = self.post(self.client, "/api/auth/register", {
            "email": "student@example.test",
            "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        self.assertEqual(status, 201)
        self.assertEqual(student["user"]["role"], "student")
        self.assertEqual(self.client.request("GET", "/api/teacher/dashboard")[0], 403)

        teacher_client = APIClient(self.base_url)
        status, _ = self.post(teacher_client, "/api/auth/register", {
            "email": "teacher@example.test",
            "display_name": "Багш",
            "password": "SecurePassword34",
            "role": "teacher",
            "teacher_invite_code": "wrong",
        })
        self.assertEqual(status, 403)
        status, teacher = self.post(teacher_client, "/api/auth/register", {
            "email": "teacher@example.test",
            "display_name": "Багш",
            "password": "SecurePassword34",
            "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        self.assertEqual(status, 201)
        self.assertEqual(teacher["user"]["role"], "teacher")
        self.assertEqual(teacher_client.request("GET", "/api/teacher/dashboard")[0], 200)

        self.post(self.client, "/api/auth/logout", {})
        self.assertIsNone(self.client.request("GET", "/api/session")[1]["user"])
        status, _ = self.post(self.client, "/api/auth/login", {
            "email": "student@example.test", "password": "SecurePassword12",
        })
        self.assertEqual(status, 200)
        reset_token = []
        with patch("server.send_password_reset", side_effect=lambda email, token: reset_token.append(token)):
            reset_status, _ = self.post(self.client, "/api/auth/password-reset/request", {
                "email": "student@example.test",
            })
        self.assertEqual(reset_status, 200)
        self.assertEqual(len(reset_token), 1)
        confirm_status, _ = self.post(self.client, "/api/auth/password-reset/confirm", {
            "token": reset_token[0], "new_password": "ChangedPassword90",
        })
        self.assertEqual(confirm_status, 200)
        self.assertIsNone(self.client.request("GET", "/api/session")[1]["user"])
        rejected_status, _ = self.post(self.client, "/api/auth/login", {
            "email": "student@example.test", "password": "SecurePassword12",
        })
        self.assertEqual(rejected_status, 401)
        accepted_status, _ = self.post(self.client, "/api/auth/login", {
            "email": "student@example.test", "password": "ChangedPassword90",
        })
        self.assertEqual(accepted_status, 200)

    def test_class_join_exercise_submission_and_persisted_teacher_report(self):
        _, teacher = self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test", "display_name": "Багш",
            "password": "SecurePassword34", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        _, classroom = self.post(self.client, "/api/classes", {"name": "12А анги"})
        self.assertEqual(len(classroom["class"]["join_code"]), 8)

        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        status, joined = self.post(student_client, "/api/classes/join", {
            "join_code": classroom["class"]["join_code"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(joined["class"]["name"], "12А анги")
        status, _ = self.post(
            self.client,
            f"/api/classes/{classroom['class']['id']}/assignments",
            {"exercise_id": "absolute-basic-01"},
        )
        self.assertEqual(status, 201)
        assignments_status, assignments = student_client.request("GET", "/api/assignments")
        self.assertEqual(assignments_status, 200)
        self.assertEqual(assignments["assignments"][0]["exercise_id"], "absolute-basic-01")
        status, _ = self.post(
            self.client,
            f"/api/classes/{classroom['class']['id']}/assessment-assignments",
            {"assessment_id": "absolute-unit-test"},
        )
        self.assertEqual(status, 201)
        test_assignments_status, test_assignments = student_client.request("GET", "/api/assessment-assignments")
        self.assertEqual(test_assignments_status, 200)
        self.assertEqual(test_assignments["assignments"][0]["assessment_id"], "absolute-unit-test")
        duplicate_status, _ = self.post(
            self.client,
            f"/api/classes/{classroom['class']['id']}/assessment-assignments",
            {"assessment_id": "absolute-unit-test"},
        )
        self.assertEqual(duplicate_status, 409)

        status, exercises = student_client.request("GET", "/api/exercises?chapter_id=absolute&level=basic")
        self.assertEqual(status, 200)
        self.assertEqual(len(exercises["exercises"]), 10)
        first = exercises["exercises"][0]
        self.assertNotIn("answer", first)
        wrong_status, wrong_result = self.post(
            student_client, f"/api/exercises/{first['id']}/attempts", {"answer": "7"}
        )
        self.assertEqual(wrong_status, 201)
        self.assertFalse(wrong_result["correct"])
        status, result = self.post(student_client, f"/api/exercises/{first['id']}/attempts", {"answer": "8"})
        self.assertEqual(status, 201)
        self.assertTrue(result["correct"])
        self.assertEqual(result["points_earned"], 1)
        polynomial_status, polynomial_exercises = student_client.request(
            "GET", "/api/exercises?chapter_id=polynomials&level=basic"
        )
        self.assertEqual(polynomial_status, 200)
        self.assertEqual(len(polynomial_exercises["exercises"]), 4)
        polynomial = next(item for item in polynomial_exercises["exercises"] if item["id"] == "polynomials-basic-04")
        status, result = self.post(
            student_client,
            f"/api/exercises/{polynomial['id']}/attempts",
            {"answer": "(x−3)(x+3)"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(result["correct"])

        for chapter_id in ("functions", "derivatives", "integrals", "probability", "complex"):
            for level in sorted(server.LEVELS):
                chapter_status, chapter_exercises = student_client.request(
                    "GET", f"/api/exercises?chapter_id={chapter_id}&level={level}"
                )
                self.assertEqual(chapter_status, 200)
                self.assertEqual(
                    len(chapter_exercises["exercises"]),
                    6 if chapter_id == "functions" else 5,
                )
                self.assertTrue(
                    all("answer" not in exercise for exercise in chapter_exercises["exercises"])
                )

        report_status, report = self.client.request("GET", "/api/teacher/dashboard")
        self.assertEqual(report_status, 200)
        self.assertEqual(report["classes"][0]["student_count"], 1)
        self.assertEqual(len(report["attempts"]), 3)
        correct_attempt = next(row for row in report["attempts"] if row["answer"] == "8")
        status, _ = self.post(
            self.client,
            f"/api/teacher/attempts/{correct_attempt['id']}/review",
            {"points": 1, "comment": "Дахин шалгасан"},
        )
        self.assertEqual(status, 200)
        report_status, report = self.client.request("GET", "/api/teacher/dashboard")
        self.assertEqual(report_status, 200)
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(student_client.request("GET", "/api/attempts")[1]["attempts"][0]["points_earned"], 1)
        dashboard_status, dashboard = student_client.request("GET", "/api/student/dashboard")
        self.assertEqual(dashboard_status, 200)
        self.assertEqual(dashboard["summary"]["studied_chapters"], 2)
        self.assertEqual(dashboard["summary"]["total_chapters"], 7)
        self.assertEqual(len(dashboard["errors"]), 1)
        self.assertEqual(dashboard["chapters"][0]["attempted_exercises"], 1)
        self.assertEqual(
            student_client.request("GET", "/api/student/errors")[1]["errors"][0]["exercise_id"],
            first["id"],
        )

        export_response = self.client.opener.open(self.base_url + "/api/teacher/report.xlsx")
        self.assertEqual(export_response.status, 200)
        with zipfile.ZipFile(io.BytesIO(export_response.read())) as workbook:
            self.assertIn("xl/workbook.xml", workbook.namelist())
            self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
            self.assertIn("Анги".encode("utf-8"), workbook.read("xl/worksheets/sheet1.xml"))
            for name in workbook.namelist():
                if name.endswith((".xml", ".rels")):
                    ET.fromstring(workbook.read(name))

        teacher_game_status, _ = self.post(self.client, "/api/games/quiz", {
            "exercise_id": "absolute-basic-01", "answer": "8", "elapsed_seconds": 4,
        })
        self.assertEqual(teacher_game_status, 403)
        game_status, game_result = self.post(student_client, "/api/games/quiz", {
            "exercise_id": "absolute-basic-01", "answer": "8", "elapsed_seconds": 4,
        })
        self.assertEqual(game_status, 200)
        self.assertTrue(game_result["correct"])
        self.assertEqual(game_result["points_earned"], 3)
        updated_dashboard = student_client.request("GET", "/api/student/dashboard")[1]
        self.assertEqual(updated_dashboard["summary"]["level"], 1)
        self.assertEqual(updated_dashboard["badges"][0]["badge_code"], "first_win")
        self.assertEqual(student_client.request("GET", "/api/leaderboard")[1]["leaderboard"][0]["points"], 3)

    def test_assessment_is_timed_persisted_and_single_submit(self):
        self.post(self.client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        status, started = self.post(self.client, "/api/assessments/absolute-unit-test/start", {})
        self.assertEqual(status, 201)
        questions = self.client.request("GET", "/api/assessments/absolute-unit-test")[1]["assessment"]["questions"]
        self.assertEqual(len(questions), 10)
        self.assertTrue(all("answer" not in question for question in questions))
        answers = {
            "absolute-basic-01": "8",
            "absolute-basic-04": "-6,6",
            "absolute-basic-07": "-2<x<4",
            "absolute-basic-10": "2<=x<=6",
            "absolute-standard-01": "-2,3",
            "absolute-standard-03": "-7<x<3",
            "absolute-standard-05": "-1",
            "absolute-standard-08": "x<-3 or x>7/3",
            "absolute-advanced-04": "3",
            "absolute-advanced-06": "x>-1",
        }
        status, result = self.post(
            self.client, f"/api/assessments/attempts/{started['attempt_id']}/submit", {"answers": answers}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["points_earned"], result["points_possible"])
        self.assertEqual(result["percentage"], 100)
        duplicate_status, _ = self.post(
            self.client, f"/api/assessments/attempts/{started['attempt_id']}/submit", {"answers": answers}
        )
        self.assertEqual(duplicate_status, 409)

    def test_unit_assessments_cover_every_chapter(self):
        chapters = server.load_curriculum()["chapters"]
        with server.connect_db() as db:
            assessments = db.execute(
                "SELECT id,chapter_id,question_ids FROM assessments WHERE published=1"
            ).fetchall()
            self.assertEqual({row["chapter_id"] for row in assessments}, {chapter["id"] for chapter in chapters})
            for assessment in assessments:
                question_ids = json.loads(assessment["question_ids"])
                self.assertEqual(len(question_ids), 10)
                self.assertEqual(len(set(question_ids)), 10)
                questions = db.execute(
                    f"SELECT id,chapter_id,level FROM exercises WHERE id IN ({','.join('?' for _ in question_ids)})",
                    question_ids,
                ).fetchall()
                self.assertEqual(len(questions), 10)
                self.assertTrue(all(question["chapter_id"] == assessment["chapter_id"] for question in questions))
                self.assertCountEqual(
                    [question["level"] for question in questions],
                    ["basic"] * 4 + ["standard"] * 4 + ["advanced"] * 2,
                )
                self.assertEqual(assessment["id"], f'{assessment["chapter_id"]}-unit-test')

        self.post(self.client, "/api/auth/register", {
            "email": "assessments@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        status, result = self.client.request("GET", "/api/assessments")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["assessments"]), len(chapters))

    def test_eesh_quiz_hides_answers_times_attempts_and_persists_result(self):
        self.post(self.client, "/api/auth/register", {
            "email": "eesh-student@example.test", "display_name": "ЭЕШ сурагч",
            "password": "SecurePassword12",
        })
        quiz = {
            "id": "eesh-2024-a",
            "year": 2024,
            "variant": "А",
            "duration_minutes": 60,
            "questions": [{
                "id": "eesh-2024-a-01",
                "prompt": "12 + 8-ыг бод.",
                "answer": "20",
                "explanation": "12 дээр 8-ыг нэмнэ.",
                "points": 2,
            }],
        }
        with patch.object(server, "load_eesh_quizzes", return_value=[quiz]):
            status, catalog = self.client.request("GET", "/api/eesh/quizzes")
            self.assertEqual(status, 200)
            self.assertNotIn("answer", catalog["quizzes"][0]["questions"][0])
            self.assertNotIn("explanation", catalog["quizzes"][0]["questions"][0])

            status, started = self.post(self.client, "/api/eesh/quizzes/eesh-2024-a/start", {})
            self.assertEqual(status, 201)
            self.assertEqual(started["duration_minutes"], 60)

            status, result = self.post(
                self.client,
                f"/api/eesh/attempts/{started['attempt_id']}/submit",
                {"answers": {"eesh-2024-a-01": "20"}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["points_earned"], 2)
            self.assertEqual(result["percentage"], 100)

            duplicate_status, _ = self.post(
                self.client,
                f"/api/eesh/attempts/{started['attempt_id']}/submit",
                {"answers": {"eesh-2024-a-01": "20"}},
            )
            self.assertEqual(duplicate_status, 409)

            with server.connect_db() as db:
                saved = db.execute(
                    "SELECT points_earned,max_points FROM eesh_quiz_attempts WHERE id=?",
                    (started["attempt_id"],),
                ).fetchone()
            self.assertEqual(tuple(saved), (2, 2))

    def test_eesh_materials_are_listed_and_only_registered_files_are_downloadable(self):
        status, result = self.client.request("GET", "/api/eesh/materials")
        self.assertEqual(status, 200)
        materials = result["materials"]
        self.assertEqual(
            [(item["year"], item["variant"]) for item in materials],
            [(2018, "А"), (2019, "А"), (2020, "Д"), (2021, "А"), (2022, "А")],
        )
        self.assertEqual([item["slide_count"] for item in materials], [43, 42, 47, 56, 54])
        self.assertTrue(all("filename" not in item for item in materials))

        with self.client.opener.open(self.base_url + materials[0]["download_url"]) as response:
            data = response.read()
            self.assertEqual(response.status, 200)
            self.assertIn("presentationml.presentation", response.headers["Content-Type"])
            self.assertIn('filename="2018-a.pptx"', response.headers["Content-Disposition"])
            self.assertTrue(data.startswith(b"PK"))

        missing_status, _ = self.client.request(
            "GET", "/api/eesh/materials/eesh-material-2099-a/file"
        )
        self.assertEqual(missing_status, 404)

    def test_eesh_material_responses_are_student_owned_and_persisted(self):
        response_path = "/api/eesh/materials/eesh-material-2018-a/responses"
        self.assertEqual(self.client.request("GET", response_path)[0], 401)
        self.assertEqual(self.post(self.client, response_path, {"slide_number": 2, "answer": "A"})[0], 401)

        self.post(self.client, "/api/auth/register", {
            "email": "student@example.test",
            "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        status, result = self.client.request("GET", response_path)
        self.assertEqual(status, 200)
        self.assertEqual(result["responses"], {})
        self.assertEqual(
            self.post(self.client, response_path, {"slide_number": 0, "answer": "A"})[0],
            400,
        )
        status, saved = self.post(
            self.client, response_path, {"slide_number": 2, "answer": "A"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved, {"saved": True, "slide_number": 2})
        self.assertEqual(self.client.request("GET", response_path)[1]["responses"], {"2": "A"})

        slide_url = self.base_url + "/api/eesh/materials/eesh-material-2018-a/slides/2"
        with self.client.opener.open(slide_url) as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertEqual(
                response.headers["Content-Disposition"],
                'inline; filename="2018-a-slide-002.jpg"',
            )
            self.assertTrue(response.read().startswith(b"\xff\xd8"))
        missing_slide_status, _ = self.client.request(
            "GET", "/api/eesh/materials/eesh-material-2018-a/slides/44"
        )
        self.assertEqual(missing_slide_status, 404)

        second_client = APIClient(self.base_url)
        self.post(second_client, "/api/auth/register", {
            "email": "second@example.test",
            "display_name": "Хоёр дахь сурагч",
            "password": "AnotherPassword12",
        })
        self.assertEqual(second_client.request("GET", response_path)[1]["responses"], {})

    def test_teacher_adds_registered_student_with_ownership_guards(self):
        self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test", "display_name": "Багш",
            "password": "SecurePassword34", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        _, classroom = self.post(self.client, "/api/classes", {"name": "12А анги"})
        class_id = classroom["class"]["id"]

        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        path = f"/api/classes/{class_id}/students"
        self.assertEqual(self.post(student_client, path, {"email": "student@example.test"})[0], 403)
        self.assertEqual(self.post(self.client, path, {"email": "bad-address"})[0], 400)
        status, result = self.post(self.client, path, {"email": " STUDENT@example.test "})
        self.assertEqual(status, 201)
        self.assertEqual(result["student"]["email"], "student@example.test")
        self.assertEqual(result["class"]["name"], "12А анги")
        self.assertEqual(self.post(self.client, path, {"email": "student@example.test"})[0], 409)
        self.assertEqual(self.post(self.client, f"/api/classes/{'a' * 32}/students", {
            "email": "student@example.test",
        })[0], 404)

        another_teacher = APIClient(self.base_url)
        self.post(another_teacher, "/api/auth/register", {
            "email": "other-teacher@example.test", "display_name": "Өөр багш",
            "password": "SecurePassword56", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        self.assertEqual(self.post(another_teacher, path, {"email": "student@example.test"})[0], 404)

    def test_teacher_removes_student_from_owned_class_without_deleting_account(self):
        self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test", "display_name": "Багш",
            "password": "SecurePassword34", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        _, class_result = self.post(self.client, "/api/classes", {"name": "12А анги"})
        classroom = class_result["class"]
        class_id = classroom["id"]

        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        self.post(self.client, f"/api/classes/{class_id}/students", {"email": "student@example.test"})
        remove_path = f"/api/classes/{class_id}/students/{student_client.request('GET', '/api/session')[1]['user']['id']}/remove"
        self.assertEqual(self.post(student_client, remove_path, {})[0], 403)

        another_teacher = APIClient(self.base_url)
        self.post(another_teacher, "/api/auth/register", {
            "email": "other-teacher@example.test", "display_name": "Өөр багш",
            "password": "SecurePassword56", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        self.assertEqual(self.post(another_teacher, remove_path, {})[0], 404)
        self.assertEqual(self.post(self.client, f"/api/classes/{class_id}/students/{'a' * 32}/remove", {})[0], 404)

        removed_status, removed = self.post(self.client, remove_path, {})
        self.assertEqual(removed_status, 200)
        self.assertIn("сурлагын мэдээлэл хэвээр үлдэнэ", removed["message"])
        self.assertEqual(self.client.request("GET", "/api/teacher/learning")[1]["students"], [])
        self.assertEqual(student_client.request("GET", "/api/session")[1]["user"]["email"], "student@example.test")
        self.assertEqual(student_client.request("GET", "/api/classes/my")[1]["classes"], [])

        self.assertEqual(self.post(student_client, "/api/classes/join", {"join_code": classroom["join_code"]})[0], 200)
        self.assertEqual(len(self.client.request("GET", "/api/teacher/learning")[1]["students"]), 1)

    def test_site_cover_is_teacher_managed_and_persisted(self):
        self.assertEqual(self.client.request("GET", "/api/site/cover"), (200, {"cover_image": None}))
        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        image = b"\x89PNG\r\n\x1a\n" + b"cover-image"
        data_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        self.assertEqual(self.post(student_client, "/api/site/cover", {"data_url": data_url})[0], 403)
        self.assertEqual(self.client.request("GET", "/api/site/cover"), (200, {"cover_image": None}))

        self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test", "display_name": "Багш",
            "password": "SecurePassword34", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        invalid_status, _ = self.post(self.client, "/api/site/cover", {
            "data_url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
        })
        self.assertEqual(invalid_status, 400)
        upload_status, upload_result = self.post(self.client, "/api/site/cover", {"data_url": data_url})
        self.assertEqual(upload_status, 200)
        image_url = upload_result["cover_image"]
        self.assertRegex(image_url, r"^/media/cover/cover-[a-f0-9]{32}\.png$")
        self.assertEqual(self.client.request("GET", "/api/site/cover")[1]["cover_image"], image_url)
        with urllib.request.urlopen(self.base_url + image_url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.read(), image)

        replacement = b"\x89PNG\r\n\x1a\n" + b"new-cover"
        replacement_url = "data:image/png;base64," + base64.b64encode(replacement).decode("ascii")
        replacement_status, replacement_result = self.post(
            self.client, "/api/site/cover", {"data_url": replacement_url}
        )
        self.assertEqual(replacement_status, 200)
        self.assertNotEqual(replacement_result["cover_image"], image_url)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base_url + image_url)
        self.assertEqual(error.exception.code, 404)

    def test_site_logo_is_teacher_managed_and_persisted(self):
        self.assertEqual(self.client.request("GET", "/api/site/logo"), (200, {"logo_image": None}))
        student_client = APIClient(self.base_url)
        self.post(student_client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        image = b"\x89PNG\r\n\x1a\n" + b"logo-image"
        data_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        self.assertEqual(self.post(student_client, "/api/site/logo", {"data_url": data_url})[0], 403)

        self.post(self.client, "/api/auth/register", {
            "email": "teacher@example.test", "display_name": "Багш",
            "password": "SecurePassword34", "role": "teacher",
            "teacher_invite_code": "TESTTEACHER42",
        })
        upload_status, upload_result = self.post(self.client, "/api/site/logo", {"data_url": data_url})
        self.assertEqual(upload_status, 200)
        image_url = upload_result["logo_image"]
        self.assertRegex(image_url, r"^/media/cover/logo-[a-f0-9]{32}\.png$")
        self.assertEqual(self.client.request("GET", "/api/site/logo")[1]["logo_image"], image_url)
        with urllib.request.urlopen(self.base_url + image_url) as response:
            self.assertEqual(response.read(), image)

        replacement = b"\x89PNG\r\n\x1a\n" + b"new-logo"
        replacement_url = "data:image/png;base64," + base64.b64encode(replacement).decode("ascii")
        replacement_status, replacement_result = self.post(
            self.client, "/api/site/logo", {"data_url": replacement_url}
        )
        self.assertEqual(replacement_status, 200)
        self.assertNotEqual(replacement_result["logo_image"], image_url)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base_url + image_url)
        self.assertEqual(error.exception.code, 404)

    def test_profile_photo_upload_and_static_file_access(self):
        self.post(self.client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        image = b"\x89PNG\r\n\x1a\n" + b"test-image"
        data_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        status, result = self.post(self.client, "/api/profile/photo", {"data_url": data_url})
        self.assertEqual(status, 200)
        image_url = result["user"]["profile_image"]
        self.assertRegex(image_url, r"^/media/profiles/profile-[a-f0-9]{32}\.png$")
        with urllib.request.urlopen(self.base_url + image_url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.read(), image)

        invalid_status, _ = self.post(self.client, "/api/profile/photo", {
            "data_url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
        })
        self.assertEqual(invalid_status, 400)
        for path in ("/media/profiles/not-an-upload.png", "/instance/platform.sqlite3", "/server.py"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(self.base_url + path)
            self.assertEqual(error.exception.code, 404)

    def test_profile_email_change_requires_password_and_preserves_session(self):
        self.post(self.client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        wrong_password_status, _ = self.post(self.client, "/api/profile/email", {
            "email": "new.student@example.test", "password": "WrongPassword12",
        })
        self.assertEqual(wrong_password_status, 401)

        invalid_email_status, _ = self.post(self.client, "/api/profile/email", {
            "email": "not-an-email", "password": "SecurePassword12",
        })
        self.assertEqual(invalid_email_status, 400)

        self.post(self.client, "/api/auth/logout", {})
        self.post(self.client, "/api/auth/register", {
            "email": "taken@example.test", "display_name": "Өөр сурагч",
            "password": "SecurePassword34",
        })
        self.client = APIClient(self.base_url)
        self.post(self.client, "/api/auth/login", {
            "email": "student@example.test", "password": "SecurePassword12",
        })
        duplicate_status, _ = self.post(self.client, "/api/profile/email", {
            "email": "taken@example.test", "password": "SecurePassword12",
        })
        self.assertEqual(duplicate_status, 409)

        changed_status, result = self.post(self.client, "/api/profile/email", {
            "email": "  NEW.Student@example.test ", "password": "SecurePassword12",
        })
        self.assertEqual(changed_status, 200)
        self.assertEqual(result["user"]["email"], "new.student@example.test")
        session_status, session = self.client.request("GET", "/api/session")
        self.assertEqual(session_status, 200)
        self.assertEqual(session["user"]["email"], "new.student@example.test")
        self.assertEqual(self.post(self.client, "/api/auth/logout", {})[0], 200)
        login_status, _ = self.post(self.client, "/api/auth/login", {
            "email": "new.student@example.test", "password": "SecurePassword12",
        })
        self.assertEqual(login_status, 200)

    def test_profile_name_change_requires_password(self):
        self.post(self.client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Хуучин нэр",
            "password": "SecurePassword12",
        })
        wrong_password_status, _ = self.post(self.client, "/api/profile/name", {
            "display_name": "Шинэ нэр", "password": "WrongPassword12",
        })
        self.assertEqual(wrong_password_status, 401)
        invalid_name_status, _ = self.post(self.client, "/api/profile/name", {
            "display_name": "А", "password": "SecurePassword12",
        })
        self.assertEqual(invalid_name_status, 400)
        changed_status, result = self.post(self.client, "/api/profile/name", {
            "display_name": "  Шинэ нэр  ", "password": "SecurePassword12",
        })
        self.assertEqual(changed_status, 200)
        self.assertEqual(result["user"]["display_name"], "Шинэ нэр")
        self.assertEqual(self.client.request("GET", "/api/session")[1]["user"]["display_name"], "Шинэ нэр")

    def test_profile_password_change_requires_current_password_and_revokes_other_sessions(self):
        self.post(self.client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч",
            "password": "SecurePassword12",
        })
        other_client = APIClient(self.base_url)
        self.assertEqual(self.post(other_client, "/api/auth/login", {
            "email": "student@example.test", "password": "SecurePassword12",
        })[0], 200)

        wrong_password_status, _ = self.post(self.client, "/api/profile/password", {
            "current_password": "WrongPassword12", "new_password": "ChangedPassword90",
        })
        self.assertEqual(wrong_password_status, 401)
        short_password_status, _ = self.post(self.client, "/api/profile/password", {
            "current_password": "SecurePassword12", "new_password": "short",
        })
        self.assertEqual(short_password_status, 400)
        same_password_status, _ = self.post(self.client, "/api/profile/password", {
            "current_password": "SecurePassword12", "new_password": "SecurePassword12",
        })
        self.assertEqual(same_password_status, 400)
        changed_status, _ = self.post(self.client, "/api/profile/password", {
            "current_password": "SecurePassword12", "new_password": "ChangedPassword90",
        })
        self.assertEqual(changed_status, 200)
        self.assertIsNotNone(self.client.request("GET", "/api/session")[1]["user"])
        self.assertIsNone(other_client.request("GET", "/api/session")[1]["user"])
        self.assertEqual(self.post(self.client, "/api/auth/logout", {})[0], 200)
        self.assertEqual(self.post(self.client, "/api/auth/login", {
            "email": "student@example.test", "password": "SecurePassword12",
        })[0], 401)
        self.assertEqual(self.post(self.client, "/api/auth/login", {
            "email": "student@example.test", "password": "ChangedPassword90",
        })[0], 200)

    def test_mutation_rejects_cross_origin_and_short_password(self):
        request = urllib.request.Request(
            self.base_url + "/api/auth/register",
            data=json.dumps({
                "email": "student@example.test", "display_name": "Сурагч", "password": "short",
            }).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)

        status, result = self.post(self.client, "/api/auth/register", {
            "email": "student@example.test", "display_name": "Сурагч", "password": "short",
        })
        self.assertEqual(status, 400)
        self.assertIn("12", result["message"])


if __name__ == "__main__":
    unittest.main()
