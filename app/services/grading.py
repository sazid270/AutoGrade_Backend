import logging
import time
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from app.models.exams import Exam, Question
from app.models.results import Result
from app.models.scanning import AnswerSheet, StudentAnswer
from app.models.students import Student
from app.services.ai_provider import validate_sheet_image
from app.services.sheet_template import TemplateMismatch, read_with_template
from app.utils.student_keys import (
    normalize_class_name,
    normalize_roll_no,
    normalize_section,
    normalize_student_key,
)

logger = logging.getLogger(__name__)


def validate_exam_ready(exam: Exam) -> None:
    if exam.exam_type != Exam.Type.MCQ:
        raise ValueError("Only MCQ exams can be scanned.")
    question_count = exam.question_count
    if question_count < 1:
        raise ValueError("Set a positive question_count on the exam before scanning.")
    question = Question.objects.filter(exam=exam).first()
    if question is None:
        raise ValueError("Add the MCQ question paper and settings before scanning.")
    answers = list(exam.answers.order_by("question_number"))
    if [answer.question_number for answer in answers] != list(
        range(1, question_count + 1)
    ):
        raise ValueError("Add an answer row for every exam question.")
    if any(answer.needs_review for answer in answers):
        raise ValueError("Review every flagged answer key before scanning.")
    total = exam.mcq_mark * question_count
    if total > exam.marks:
        raise ValueError(
            f"The answer key is worth {total} marks ({question_count} questions "
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
    maximum = exam.mcq_mark * exam.question_count
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
    result.save(
        update_fields=("sheet", "mcq_marks", "cq_marks", "updated_at")
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


def process_sheet(sheet_id: int) -> AnswerSheet:
    initial = AnswerSheet.objects.select_related(
        "batch__exam",
        "batch__template",
        "image",
    ).get(pk=sheet_id)
    exam = initial.batch.exam
    validate_sheet_image(initial.image)
    validate_exam_ready(exam)
    question = Question.objects.get(exam=exam)
    question_numbers = list(range(1, exam.question_count + 1))
    started = time.monotonic()
    layout_error = ""
    try:
        if initial.batch.template_id is None:
            raise TemplateMismatch("No OMR template is assigned to this batch.")
        read = read_with_template(
            initial,
            initial.batch.template.structure,
            question_numbers,
            multi_allowed={
                number: question.allow_multiple for number in question_numbers
            },
        )
    except TemplateMismatch as exc:
        layout_error = str(exc)
        logger.info("Sheet %s requires review: %s", sheet_id, layout_error)
        read = {
            "answers": {number: [] for number in question_numbers},
            "unclear": [],
            "unclear_fields": set(),
            "name": "",
            "roll": "",
            "registration": "",
            "class_name": "",
            "section": "",
            "subject_code": "",
            "set_code": "",
        }
    logger.info(
        "Sheet %s read by OpenCV in %.1fs",
        sheet_id,
        time.monotonic() - started,
    )
    if layout_error:
        reasons = [{"field": "layout_mismatch", "detail": layout_error}]
    else:
        reasons = [
            {"field": "answer", "question_number": number} for number in read["unclear"]
        ]
        if sum(1 for options in read["answers"].values() if not options) * 2 > len(
            question_numbers
        ):
            reasons.append({"field": "many_blank_answers"})
    unclear = read["unclear_fields"]
    exam_section = normalize_section(exam.section)
    sheet_section = normalize_section(read["section"]) or normalize_section(
        exam.section
    )
    exam_class = normalize_class_name(exam.class_or_batch)
    sheet_class = normalize_class_name(read["class_name"]) or exam_class
    if "roll" in unclear:
        reasons.append({"field": "roll"})
    if "registration" in unclear:
        reasons.append({"field": "registration"})
    identity_unreliable = "roll" in unclear
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
                },
            )
            _update_result_from_sheet(result, sheet)
            from app.tasks import _record_sheet_completion

            _record_sheet_completion(sheet.batch_id)
    return sheet


@transaction.atomic
def resolve_sheet(sheet_id: int, user) -> AnswerSheet:
    sheet = (
        AnswerSheet.objects.select_for_update()
        .select_related("batch__exam", "student")
        .get(pk=sheet_id)
    )
    if sheet.batch.uploaded_by_id != user.id:
        raise ValueError("You cannot resolve another user's sheet.")
    if sheet.status != AnswerSheet.Status.NEEDS_REVIEW:
        raise ValueError("Only sheets that need review can be resolved.")
    exam = sheet.batch.exam
    student = sheet.student
    if student is None:
        raise ValueError("Select a student for this sheet.")
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
        },
    )
    _update_result_from_sheet(result, sheet)
    from app.tasks import _record_sheet_completion

    _record_sheet_completion(sheet.batch_id)
    return sheet


@transaction.atomic
def discard_sheet(sheet_id: int, user) -> AnswerSheet:
    sheet = (
        AnswerSheet.objects.select_for_update().select_related("batch").get(pk=sheet_id)
    )
    if sheet.batch.uploaded_by_id != user.id:
        raise ValueError("You cannot discard another user's sheet.")
    if sheet.status == AnswerSheet.Status.PENDING:
        raise ValueError("A sheet that is still processing cannot be discarded.")
    for result in Result.objects.select_for_update().filter(sheet=sheet):
        if result.finalized or result.is_overridden:
            raise ValueError(
                "This sheet has a finalized or edited result. Edit the result instead."
            )
    Result.objects.filter(sheet=sheet).delete()
    sheet.student = None
    sheet.status = AnswerSheet.Status.DISCARDED
    sheet.review_reasons = []
    sheet.error_message = "Discarded by the teacher."
    sheet.save(update_fields=("student", "status", "review_reasons", "error_message"))
    from app.tasks import _record_sheet_completion

    _record_sheet_completion(sheet.batch_id)
    return sheet
