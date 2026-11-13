from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.utils import timezone

from app.models.exams import AnswerKey, Exam, Question
from app.models.results import Result
from app.models.scanning import AnswerSheet, StudentAnswer
from app.models.students import Student
from app.utils.omr import process_image
from app.utils.student_keys import normalize_roll_no, normalize_student_key


def grade_answer(
    question: Question,
    selected_options: list[str],
    answer_key: AnswerKey | None,
    exam: Exam,
) -> tuple[bool | None, Decimal]:
    if answer_key is None:
        return None, Decimal("0")
    selected = sorted({str(option).strip().upper() for option in selected_options})
    correct = selected == sorted(answer_key.correct_options)
    if correct:
        return True, question.marks
    if selected:
        return False, -exam.negative_marking
    return False, Decimal("0")


def validate_exam_template(exam: Exam) -> None:
    expected_questions = set(
        exam.questions.filter(type=Question.Type.MCQ).values_list("number", flat=True)
    )
    template_questions = {
        int(item["number"])
        for item in exam.omr_template.get("answers", [])
        if isinstance(item, dict) and "number" in item
    }
    answers = exam.omr_template.get("answers", [])
    roll_columns = exam.omr_template.get("roll_number", {}).get("columns", [])
    if (
        (expected_questions and not answers)
        or not roll_columns
        or "asymmetric_marker_corner" not in exam.omr_template
        or expected_questions != template_questions
    ):
        raise ValueError("The OMR template does not match the exam MCQ questions.")
    if len(template_questions) != len(answers):
        raise ValueError("The OMR template contains duplicate question numbers.")
    if any(len(column.get("bubbles", [])) != 10 for column in roll_columns):
        raise ValueError("Every roll-number column must contain 10 bubbles.")
    question_total = sum(
        question.marks for question in exam.questions.all()
    )
    if question_total != exam.total_marks:
        raise ValueError("Exam total_marks must equal the sum of question marks.")
    corner = exam.omr_template["asymmetric_marker_corner"]
    if not isinstance(corner, int) or not 0 <= corner < 4:
        raise ValueError("asymmetric_marker_corner must be between 0 and 3.")
    questions = {
        question.number: question
        for question in exam.questions.filter(type=Question.Type.MCQ).all()
    }
    for item in answers:
        question = questions[int(item["number"])]
        if len(item.get("bubbles", [])) != question.options_count:
            raise ValueError("OMR bubble count does not match question options.")
        if bool(item.get("allow_multiple", False)) != question.allow_multiple:
            raise ValueError("OMR multiple-selection setting does not match question.")
        if not hasattr(question, "answer_key"):
            raise ValueError("Every MCQ must have an answer key before scanning.")


def _update_result(result: Result) -> None:
    result.recalculate().save(
        update_fields=("mcq_marks", "cq_marks", "total_marks", "updated_at")
    )


def regrade_sheet(sheet: AnswerSheet) -> None:
    exam = sheet.batch.exam
    answers = (
        sheet.answers.select_for_update()
        .select_related("question")
        .prefetch_related(Prefetch("question__answer_key"))
    )
    for answer in answers:
        if answer.is_overridden:
            continue
        answer.is_correct, answer.marks_awarded = grade_answer(
            answer.question,
            answer.selected_options,
            getattr(answer.question, "answer_key", None),
            exam,
        )
        answer.save(update_fields=("is_correct", "marks_awarded"))


def process_sheet(sheet_id: int) -> AnswerSheet:
    initial_sheet = AnswerSheet.objects.select_related("batch__exam__created_by").get(
        pk=sheet_id
    )
    data = process_image(
        initial_sheet.image.path,
        initial_sheet.batch.exam.omr_template,
    )

    with transaction.atomic():
        sheet = (
            AnswerSheet.objects.select_for_update()
            .select_related("batch__exam__created_by")
            .get(pk=sheet_id)
        )
        exam = sheet.batch.exam
        detected_roll_no = data["roll_no"] or ""
        roll_no = normalize_roll_no(detected_roll_no)
        student = None
        duplicate = False
        if roll_no and data["roll_flag"] == "ok":
            student_key = {
                "created_by": exam.created_by,
                "institution": normalize_student_key(exam.institution),
                "grade": normalize_student_key(exam.grade),
                "section": normalize_student_key(exam.section),
                "academic_year": normalize_student_key(exam.academic_year),
                "roll_no": roll_no,
            }
            try:
                with transaction.atomic():
                    student, _ = Student.objects.select_for_update().get_or_create(
                        **student_key,
                        defaults={
                            "created_via": Student.CreatedVia.SCAN,
                            "status": Student.Status.NEEDS_COMPLETION,
                        },
                    )
            except IntegrityError:
                student = Student.objects.select_for_update().get(**student_key)
            duplicate = (
                AnswerSheet.objects.filter(
                    batch__exam=exam,
                    student=student,
                )
                .exclude(pk=sheet.pk)
                .exists()
            )
            if duplicate:
                AnswerSheet.objects.filter(
                    batch__exam=exam,
                    student=student,
                ).update(status=AnswerSheet.Status.NEEDS_REVIEW)
        else:
            roll_no = ""

        sheet.student = student
        sheet.detected_roll_no = normalize_roll_no(detected_roll_no)
        sheet.roll_confidence = data["roll_confidence"]
        sheet.status = (
            AnswerSheet.Status.PROCESSED
            if not duplicate
            and data["roll_flag"] == "ok"
            and all(
                answer["flag"] not in {"multiple", "low_confidence"}
                for answer in data["answers"].values()
            )
            else AnswerSheet.Status.NEEDS_REVIEW
        )
        sheet.error_message = (
            "Duplicate roll number detected for this exam." if duplicate else ""
        )
        sheet.processed_at = timezone.now()
        sheet.save(
            update_fields=(
                "student",
                "detected_roll_no",
                "roll_confidence",
                "status",
                "error_message",
                "processed_at",
            )
        )

        questions = {
            question.number: question
            for question in exam.questions.select_related("answer_key").all()
        }
        for number, answer_data in data["answers"].items():
            question = questions[int(number)]
            key = getattr(question, "answer_key", None)
            existing = StudentAnswer.objects.filter(
                sheet=sheet, question=question
            ).first()
            if existing and existing.is_overridden:
                continue
            is_correct, marks = grade_answer(
                question,
                answer_data["selected_options"],
                key,
                exam,
            )
            StudentAnswer.objects.update_or_create(
                sheet=sheet,
                question=question,
                defaults={
                    "selected_options": answer_data["selected_options"],
                    "fill_ratios": answer_data["fill_ratios"],
                    "is_correct": is_correct,
                    "marks_awarded": marks,
                    "flag": answer_data["flag"],
                    "confidence": answer_data["confidence"],
                    "is_overridden": False,
                },
            )

        if student and not duplicate:
            result, _ = Result.objects.get_or_create(
                exam=exam,
                student=student,
                defaults={"sheet": sheet},
            )
            if not result.finalized and not result.is_overridden:
                result.sheet = sheet
                _update_result(result)
    return sheet


@transaction.atomic
def regrade_result(result_id: int) -> Result:
    result = (
        Result.objects.select_for_update()
        .select_related("exam")
        .get(pk=result_id)
    )
    if result.finalized or result.is_overridden or not result.sheet_id:
        return result
    exam = result.exam
    sheet = AnswerSheet.objects.select_for_update().get(pk=result.sheet_id)
    for answer in sheet.answers.select_related("question__answer_key").all():
        if answer.is_overridden:
            continue
        key = getattr(answer.question, "answer_key", None)
        answer.is_correct, answer.marks_awarded = grade_answer(
            answer.question,
            answer.selected_options,
            key,
            exam,
        )
        answer.save(update_fields=("is_correct", "marks_awarded"))
    result.sheet = sheet
    _update_result(result)
    return result


def regrade_exam(exam_id: int) -> int:
    sheet_ids = AnswerSheet.objects.filter(batch__exam_id=exam_id).values_list(
        "pk", flat=True
    )
    for sheet_id in sheet_ids.iterator():
        with transaction.atomic():
            sheet = (
                AnswerSheet.objects.select_for_update()
                .select_related("batch__exam")
                .get(pk=sheet_id)
            )
            regrade_sheet(sheet)
            result = sheet.result.filter(finalized=False).first()
            if result and not result.is_overridden:
                result.sheet = sheet
                _update_result(result)
    return len(sheet_ids)


@transaction.atomic
def resolve_sheet(
    sheet_id: int,
    student_id: int,
    user,
) -> AnswerSheet:
    sheet = (
        AnswerSheet.objects.select_for_update()
        .select_related("batch__exam")
        .get(pk=sheet_id)
    )
    if sheet.batch.uploaded_by_id != user.id:
        raise ValueError("You cannot resolve another user's sheet.")
    if sheet.status != AnswerSheet.Status.NEEDS_REVIEW or not sheet.answers.exists():
        raise ValueError("Only a reviewed sheet with answers can be resolved.")
    if sheet.answers.filter(
        flag__in=("multiple", "low_confidence"), is_overridden=False
    ).exists():
        raise ValueError("Resolve all multiple or low-confidence answers first.")
    student = (
        Student.objects.select_for_update()
        .filter(
            pk=student_id,
            created_by_id=user.id,
        )
        .first()
    )
    if student is None:
        raise ValueError("The selected student does not belong to your account.")
    exam = sheet.batch.exam
    if (
        normalize_student_key(student.institution)
        != normalize_student_key(exam.institution)
        or normalize_student_key(student.grade) != normalize_student_key(exam.grade)
        or normalize_student_key(student.section) != normalize_student_key(exam.section)
        or normalize_student_key(student.academic_year)
        != normalize_student_key(exam.academic_year)
    ):
        raise ValueError("The student does not match the exam context.")
    if (
        AnswerSheet.objects.filter(
            batch__exam=sheet.batch.exam,
            student=student,
        )
        .exclude(pk=sheet.pk)
        .exists()
    ):
        raise ValueError("A sheet for this student and exam already exists.")
    sheet.student = student
    sheet.status = AnswerSheet.Status.PROCESSED
    sheet.error_message = ""
    sheet.save(update_fields=("student", "status", "error_message"))
    regrade_sheet(sheet)
    result, _ = Result.objects.get_or_create(
        exam=sheet.batch.exam,
        student=student,
        defaults={"sheet": sheet},
    )
    if not result.finalized and not result.is_overridden:
        result.sheet = sheet
        _update_result(result)
    return sheet


@transaction.atomic
def keep_sheet(sheet_id: int, user) -> AnswerSheet:
    winner = (
        AnswerSheet.objects.select_for_update()
        .select_related("batch__exam")
        .get(pk=sheet_id)
    )
    if winner.batch.uploaded_by_id != user.id or winner.student_id is None:
        raise ValueError("The winning sheet must belong to you and have a student.")
    if winner.answers.filter(
        flag__in=("multiple", "low_confidence"), is_overridden=False
    ).exists():
        raise ValueError("Resolve all multiple or low-confidence answers first.")
    losers = list(
        AnswerSheet.objects.select_for_update()
        .filter(batch__exam=winner.batch.exam, student_id=winner.student_id)
        .exclude(pk=winner.pk)
    )
    if not losers:
        raise ValueError("No duplicate sheet exists for this student and exam.")
    for loser in losers:
        for result in Result.objects.select_for_update().filter(sheet=loser):
            if result.finalized or result.is_overridden:
                raise ValueError(
                    "A duplicate sheet has a finalized or overridden result."
                )
        loser.student = None
        loser.status = AnswerSheet.Status.DISCARDED
        loser.error_message = "Discarded because another duplicate sheet was kept."
        loser.save(update_fields=("student", "status", "error_message"))
        loser.result.all().delete()
    winner.status = AnswerSheet.Status.PROCESSED
    winner.error_message = ""
    winner.save(update_fields=("status", "error_message"))
    regrade_sheet(winner)
    result, _ = Result.objects.get_or_create(
        exam=winner.batch.exam,
        student=winner.student,
        defaults={"sheet": winner},
    )
    if not result.finalized and not result.is_overridden:
        result.sheet = winner
        _update_result(result)
    from app.tasks import _record_sheet_completion

    _record_sheet_completion(winner.batch_id)
    return winner
