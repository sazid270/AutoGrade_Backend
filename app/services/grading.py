import logging
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from app.models.exams import Exam
from app.models.results import Result
from app.models.scanning import AnswerSheet, StudentAnswer
from app.models.students import Student
from app.services.ai_provider import (
    AIProviderError,
    ProviderTransientError,
    free_vision_models,
    validate_sheet_image,
    validate_sheet_reading_config,
)
from app.services.sheet_reading import read_answer_sheet
from app.utils.options import BENGALI_DIGITS
from app.utils.student_keys import (
    normalize_class_name,
    normalize_roll_no,
    normalize_section,
    normalize_student_key,
)

logger = logging.getLogger(__name__)
LAST_GOOD_MODEL_KEY = "openrouter_last_good_sheet_model"


def _code_key(value: str | None) -> str:
    return normalize_student_key(value).translate(BENGALI_DIGITS)


def validate_exam_ready(exam: Exam) -> None:
    if exam.exam_type != Exam.Type.MCQ:
        raise ValueError("Only MCQ exams can be scanned.")
    questions = list(exam.questions.order_by("pk"))
    if not questions:
        raise ValueError("Add at least one MCQ question before scanning sheets.")
    answers = list(exam.answers.order_by("question_number"))
    if len(answers) != len(questions):
        raise ValueError("Add an answer row for every exam question.")
    if any(answer.needs_review for answer in answers):
        raise ValueError("Review every flagged answer key before scanning.")
    if any(
        answer.question_id != questions[index].pk
        for index, answer in enumerate(answers)
    ):
        raise ValueError("Every MCQ must have a matching answer key.")
    total = exam.mcq_mark * len(questions)
    if total > exam.marks:
        raise ValueError(
            f"The answer key is worth {total} marks ({len(questions)} questions "
            f"x {exam.mcq_mark}) but the exam total is {exam.marks}. "
            "Fix the exam marks or per-question mark."
        )


def calculate_mcq_marks(exam: Exam, selected_answers: dict[int, list[str]]) -> Decimal:
    rows = list(exam.answers.order_by("question_number"))
    if not rows:
        raise ValueError("Add at least one MCQ answer before grading.")
    penalty = Decimal(str(exam.negative_marks))
    score = Decimal("0")
    for row in rows:
        selected = selected_answers.get(row.question_number, [])
        if not selected:
            continue
        if sorted(set(selected)) == sorted(set(row.correct_options)):
            score += exam.mcq_mark
        else:
            score -= penalty
    maximum = exam.mcq_mark * len(rows)
    return min(maximum, max(Decimal("0"), score)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _selected_answers(sheet: AnswerSheet) -> dict[int, list[str]]:
    return {
        answer.question_number: answer.selected_options
        for answer in sheet.answers.all()
    }


def _update_result_from_sheet(result: Result, sheet: AnswerSheet) -> None:
    if result.finalized or result.is_overridden:
        return
    result.sheet = sheet
    result.mcq_marks = sheet.scanned_mcq_marks
    result.cq_marks = None
    result.recalculate()
    result.save(
        update_fields=("sheet", "mcq_marks", "cq_marks", "total_marks", "updated_at")
    )


def recompute_sheet(sheet_id: int) -> AnswerSheet:
    with transaction.atomic():
        sheet = (
            AnswerSheet.objects.select_for_update()
            .select_related("batch__exam")
            .get(pk=sheet_id)
        )
        exam = sheet.batch.exam
        sheet.scanned_mcq_marks = calculate_mcq_marks(
            exam,
            _selected_answers(sheet),
        )
        sheet.save(update_fields=("scanned_mcq_marks",))
        if sheet.student_id and sheet.status == AnswerSheet.Status.PROCESSED:
            result = (
                Result.objects.select_for_update()
                .filter(exam=exam, student_id=sheet.student_id)
                .first()
            )
            if result:
                _update_result_from_sheet(result, sheet)
    return sheet


def regrade_exam(exam: Exam) -> int:
    sheet_ids = list(
        AnswerSheet.objects.filter(
            batch__exam=exam,
            status__in=(
                AnswerSheet.Status.PROCESSED,
                AnswerSheet.Status.NEEDS_REVIEW,
            ),
        ).values_list("pk", flat=True)
    )
    for sheet_id in sheet_ids:
        recompute_sheet(sheet_id)
    return len(sheet_ids)


def _read_with_fallback(
    image,
    *,
    question_numbers: list[int],
    options_count: dict[int, int],
) -> tuple[str, dict[str, Any]]:
    models = free_vision_models()
    preferred = cache.get(LAST_GOOD_MODEL_KEY)
    if preferred in models:
        models = [preferred] + [model for model in models if model != preferred]
    last_error = None
    for model in models[:3]:
        try:
            result = read_answer_sheet(
                image,
                question_numbers=question_numbers,
                options_count=options_count,
                model=model,
            )
        except ProviderTransientError as exc:
            if exc.status_code == 429:
                raise
            last_error = exc
            if exc.status_code == 404:
                free_vision_models(refresh=True)
        except AIProviderError as exc:
            last_error = exc
            if exc.status_code == 404:
                free_vision_models(refresh=True)
        else:
            cache.set(LAST_GOOD_MODEL_KEY, model, timeout=6 * 3600)
            return model, result
    raise last_error or ProviderTransientError("No free vision model is available.")


def process_sheet(sheet_id: int) -> AnswerSheet:
    initial = AnswerSheet.objects.select_related("batch__exam", "image").get(
        pk=sheet_id
    )
    exam = initial.batch.exam
    validate_sheet_reading_config()
    validate_sheet_image(initial.image)
    validate_exam_ready(exam)
    questions = list(exam.questions.order_by("pk"))
    question_numbers = list(range(1, len(questions) + 1))
    options_count = {
        number: question.options_count
        for number, question in zip(question_numbers, questions)
    }
    started = time.monotonic()
    model, read = _read_with_fallback(
        initial.image,
        question_numbers=question_numbers,
        options_count=options_count,
    )
    logger.info(
        "Sheet %s read by %s in %.1fs",
        sheet_id,
        model,
        time.monotonic() - started,
    )
    reasons = [
        {"field": "answer", "question_number": number} for number in read["unclear"]
    ]
    if sum(1 for options in read["answers"].values() if not options) * 2 > len(
        question_numbers
    ):
        reasons.append({"field": "many_blank_answers"})
    unclear = read["unclear_fields"]
    exam_section = normalize_section(exam.section)
    sheet_section = normalize_section(read["section"])
    exam_class = normalize_class_name(exam.class_or_batch)
    sheet_class = normalize_class_name(read["class_name"])
    section_bad = bool(
        (exam_section and sheet_section and sheet_section != exam_section)
        or "section" in unclear
    )
    class_bad = bool(
        (exam_class and sheet_class and sheet_class != exam_class)
        or "class_name" in unclear
    )
    if section_bad:
        reasons.append(
            {"field": "section", "read": sheet_section, "expected": exam_section}
        )
    if class_bad:
        reasons.append({"field": "class", "read": sheet_class, "expected": exam_class})
    if "registration" in unclear:
        reasons.append({"field": "registration"})
    identity_unreliable = section_bad or class_bad or "roll" in unclear
    section = exam_section or sheet_section
    class_name = exam_class or sheet_class
    class_key = class_name

    with transaction.atomic():
        sheet = (
            AnswerSheet.objects.select_for_update()
            .select_related("batch__exam")
            .get(pk=sheet_id)
        )
        exam = sheet.batch.exam
        roll_no = normalize_roll_no(read["roll"])
        registration_no = normalize_student_key(read["registration"])
        student = None

        if roll_no and not identity_unreliable:
            context = {
                "created_by": exam.created_by,
                "institution": normalize_student_key(exam.institution),
                "section": section,
                "academic_year": normalize_student_key(exam.exam_year),
            }
            candidates = (
                Student.objects.select_for_update()
                .filter(**context, roll_no=roll_no)
                .order_by("pk")
            )
            student = next(
                (
                    candidate
                    for candidate in candidates
                    if normalize_class_name(candidate.class_name) == class_key
                ),
                None,
            )
            if student is None and registration_no:
                student = (
                    Student.objects.select_for_update()
                    .filter(
                        created_by=exam.created_by,
                        institution=normalize_student_key(exam.institution),
                        registration_no__iexact=registration_no,
                    )
                    .first()
                )
            if student is None:
                try:
                    student, _ = Student.objects.select_for_update().get_or_create(
                        **context,
                        roll_no=roll_no,
                        class_name=class_name,
                        defaults={
                            "registration_no": registration_no,
                            "name": read["name"] or "",
                            "created_via": Student.CreatedVia.SCAN,
                            "status": Student.Status.NEEDS_COMPLETION,
                        },
                    )
                except IntegrityError:
                    student = (
                        Student.objects.select_for_update()
                        .filter(
                            created_by=exam.created_by,
                            roll_no=roll_no,
                            institution=normalize_student_key(exam.institution),
                            section=section,
                            academic_year=normalize_student_key(exam.exam_year),
                        )
                        .order_by("pk")
                    )
                    student = next(
                        (
                            candidate
                            for candidate in student
                            if normalize_class_name(candidate.class_name) == class_key
                        ),
                        None,
                    )

            if student:
                mismatched = (
                    normalize_student_key(student.institution)
                    != normalize_student_key(exam.institution)
                    or normalize_class_name(student.class_name) != class_key
                    or normalize_section(student.section) != section
                    or normalize_student_key(student.academic_year)
                    != normalize_student_key(exam.exam_year)
                    or normalize_roll_no(student.roll_no) != roll_no
                    or (
                        registration_no
                        and student.registration_no
                        and normalize_student_key(student.registration_no)
                        != registration_no
                    )
                )
                if mismatched:
                    reasons.append({"field": "student_context"})

        subject_code = _code_key(read["subject_code"])
        exam_subject_code = _code_key(exam.subject_code)
        if exam_subject_code and subject_code and subject_code != exam_subject_code:
            reasons.append(
                {
                    "field": "subject_code",
                    "read": subject_code,
                    "expected": exam_subject_code,
                }
            )

        set_code = normalize_section(read["set_code"])
        exam_set_code = normalize_section(exam.set_code)
        if exam_set_code and set_code and set_code != exam_set_code:
            reasons.append(
                {
                    "field": "set_code",
                    "read": set_code,
                    "expected": exam_set_code,
                }
            )

        duplicate = bool(
            student
            and AnswerSheet.objects.filter(
                batch__exam=exam,
                student=student,
            )
            .exclude(pk=sheet.pk)
            .exclude(status=AnswerSheet.Status.DISCARDED)
            .exists()
        )
        if duplicate:
            reasons.append({"field": "duplicate_student"})
        if not student:
            reasons.append(
                {
                    "field": "student",
                    "read": {
                        "name": read["name"],
                        "roll": roll_no,
                        "registration": registration_no,
                        "class": read["class_name"],
                        "section": sheet_section,
                    },
                }
            )

        sheet.student = student
        sheet.review_reasons = reasons
        sheet.scanned_mcq_marks = calculate_mcq_marks(
            exam,
            read["answers"],
        )
        sheet.status = (
            AnswerSheet.Status.PROCESSED
            if student and not reasons and not duplicate
            else AnswerSheet.Status.NEEDS_REVIEW
        )
        sheet.error_message = ""
        sheet.processed_at = timezone.now()
        sheet.save(
            update_fields=(
                "student",
                "review_reasons",
                "scanned_mcq_marks",
                "status",
                "error_message",
                "processed_at",
            )
        )
        StudentAnswer.objects.filter(sheet=sheet).delete()
        StudentAnswer.objects.bulk_create(
            [
                StudentAnswer(
                    sheet=sheet,
                    question_number=number,
                    selected_options=read["answers"][number],
                    needs_review=number in read["unclear"],
                )
                for number in question_numbers
            ]
        )

        if sheet.status == AnswerSheet.Status.PROCESSED:
            result, _ = Result.objects.get_or_create(
                exam=exam,
                student=student,
                defaults={
                    "sheet": sheet,
                    "mcq_marks": sheet.scanned_mcq_marks,
                    "total_marks": sheet.scanned_mcq_marks,
                },
            )
            _update_result_from_sheet(result, sheet)
            from app.tasks import _record_sheet_completion

            _record_sheet_completion(sheet.batch_id)
    return sheet


@transaction.atomic
def resolve_sheet(sheet_id: int, student_id: int, user) -> AnswerSheet:
    sheet = (
        AnswerSheet.objects.select_for_update()
        .select_related("batch__exam")
        .get(pk=sheet_id)
    )
    if sheet.batch.uploaded_by_id != user.id:
        raise ValueError("You cannot resolve another user's sheet.")
    if sheet.status != AnswerSheet.Status.NEEDS_REVIEW:
        raise ValueError("Only sheets that need review can be resolved.")
    exam = sheet.batch.exam
    if student_id is None:
        student = sheet.student
        if student is None:
            raise ValueError("Select a student for this sheet.")
    else:
        student = (
            Student.objects.select_for_update()
            .filter(pk=student_id, created_by_id=user.id)
            .first()
        )
        if student is None:
            raise ValueError("The selected student does not belong to your account.")
    exam_section = normalize_section(exam.section)
    if (
        normalize_student_key(student.institution)
        != normalize_student_key(exam.institution)
        or normalize_class_name(student.class_name)
        != normalize_class_name(exam.class_or_batch)
        or normalize_student_key(student.academic_year)
        != normalize_student_key(exam.exam_year)
        or (exam_section and normalize_section(student.section) != exam_section)
    ):
        raise ValueError("The student does not match the exam context.")
    if (
        AnswerSheet.objects.filter(batch__exam=exam, student=student)
        .exclude(pk=sheet.pk)
        .exists()
    ):
        raise ValueError("A sheet for this student and exam already exists.")
    sheet.student = student
    sheet.status = AnswerSheet.Status.PROCESSED
    sheet.review_reasons = []
    sheet.error_message = ""
    sheet.scanned_mcq_marks = calculate_mcq_marks(
        exam,
        _selected_answers(sheet),
    )
    sheet.save(
        update_fields=(
            "student",
            "status",
            "review_reasons",
            "error_message",
            "scanned_mcq_marks",
        )
    )
    sheet.answers.update(needs_review=False)
    result, _ = Result.objects.get_or_create(
        exam=exam,
        student=student,
        defaults={
            "sheet": sheet,
            "mcq_marks": sheet.scanned_mcq_marks,
            "total_marks": sheet.scanned_mcq_marks,
        },
    )
    _update_result_from_sheet(result, sheet)
    from app.tasks import _record_sheet_completion

    _record_sheet_completion(sheet.batch_id)
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
    losers = list(
        AnswerSheet.objects.select_for_update()
        .filter(batch__exam=winner.batch.exam, student_id=winner.student_id)
        .exclude(pk=winner.pk)
    )
    if not losers:
        raise ValueError("No duplicate sheet exists for this student and exam.")
    affected_batch_ids = {winner.batch_id}
    for loser in losers:
        affected_batch_ids.add(loser.batch_id)
        for result in Result.objects.select_for_update().filter(sheet=loser):
            if result.finalized or result.is_overridden:
                raise ValueError(
                    "A duplicate sheet has a finalized or overridden result."
                )
        loser.student = None
        loser.status = AnswerSheet.Status.DISCARDED
        loser.error_message = "Discarded because another duplicate sheet was kept."
        loser.save(update_fields=("student", "status", "error_message"))
        Result.objects.filter(sheet=loser).delete()
    winner.status = AnswerSheet.Status.PROCESSED
    winner.review_reasons = []
    winner.error_message = ""
    winner.save(update_fields=("status", "review_reasons", "error_message"))
    result, _ = Result.objects.get_or_create(
        exam=winner.batch.exam,
        student=winner.student,
        defaults={
            "sheet": winner,
            "mcq_marks": winner.scanned_mcq_marks,
            "total_marks": winner.scanned_mcq_marks,
        },
    )
    _update_result_from_sheet(result, winner)
    from app.tasks import _record_sheet_completion

    for batch_id in affected_batch_ids:
        _record_sheet_completion(batch_id)
    return winner
