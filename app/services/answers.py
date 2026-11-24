from app.models.exams import Exam, Question


def ensure_exam_question(
    exam: Exam,
    answers_by_number: dict[int, list[str]],
) -> Question:
    question = Question.objects.filter(exam=exam).first()
    if question is not None:
        return question

    options_count = max(
        4,
        max(
            (
                ord(option.upper()) - ord("A") + 1
                for options in answers_by_number.values()
                for option in options
            ),
            default=4,
        ),
    )
    return Question.objects.create(
        exam=exam,
        options_count=options_count,
        allow_multiple=any(
            len(set(options)) > 1 for options in answers_by_number.values()
        ),
    )
