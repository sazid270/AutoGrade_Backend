from app.models.exams import Exam, Question


def ensure_exam_questions(
    exam: Exam,
    answers_by_number: dict[int, list[str]],
) -> list[Question]:
    questions = list(exam.questions.order_by("pk"))
    maximum_number = max(answers_by_number, default=len(questions))

    while len(questions) < maximum_number:
        question_number = len(questions) + 1
        options = answers_by_number.get(question_number, [])
        options_count = max(
            4,
            max(
                (ord(option.upper()) - ord("A") + 1 for option in options),
                default=4,
            ),
        )
        question = Question.objects.create(
            exam=exam,
            options_count=options_count,
            allow_multiple=len(set(options)) > 1,
        )
        questions.append(question)

    return questions
