document.querySelectorAll("[data-quiz]").forEach((quiz) => {
  const correctAnswer = quiz.dataset.answer;
  const feedback = quiz.querySelector(".feedback");

  quiz.querySelectorAll(".choice").forEach((button) => {
    button.addEventListener("click", () => {
      quiz.querySelectorAll(".choice").forEach((item) => {
        item.classList.remove("correct", "incorrect");
      });

      const isCorrect = button.dataset.choice === correctAnswer;
      button.classList.add(isCorrect ? "correct" : "incorrect");
      feedback.textContent = isCorrect
        ? `答对了。${quiz.dataset.correctFeedback}`
        : `再想一下。${quiz.dataset.retryFeedback}`;
    });
  });
});
