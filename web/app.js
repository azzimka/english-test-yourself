// --- Telegram Mini App Engine for English Tests ---
document.addEventListener("DOMContentLoaded", () => {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.expand();
    tg.ready();
    // Match Telegram theme if supported
    if (tg.setHeaderColor) tg.setHeaderColor("#FFFFFF");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#FFFFFF");
  }

  // State
  let testData = null;
  let studentData = null;
  const userAnswers = {}; // { question_id: answer_text }
  const startTime = Date.now();

  // Elements
  const elTitle = document.getElementById("test-title");
  const elBadge = document.getElementById("student-badge");
  const elProgressBar = document.getElementById("progress-bar");
  const elLoading = document.getElementById("loading");
  const elErrorScreen = document.getElementById("error-screen");
  const elErrorTitle = document.getElementById("error-title");
  const elErrorDesc = document.getElementById("error-desc");
  const elQuizContainer = document.getElementById("quiz-container");
  const elFooter = document.getElementById("footer");
  const elAnsweredCounter = document.getElementById("answered-counter");
  const elSubmitBtn = document.getElementById("submit-btn");
  const elConfirmModal = document.getElementById("confirm-modal");
  const elModalDesc = document.getElementById("modal-desc");
  const elModalCancel = document.getElementById("modal-cancel-btn");
  const elModalConfirm = document.getElementById("modal-confirm-btn");
  const elSuccessScreen = document.getElementById("success-screen");
  const elCloseBtn = document.getElementById("close-btn");

  // Parse URL search params
  const urlParams = new URLSearchParams(window.location.search);
  const testId = urlParams.get("test_id");
  const studentTgId = urlParams.get("student_id") || (tg?.initDataUnsafe?.user?.id ? String(tg.initDataUnsafe.user.id) : null);

  if (!testId || !studentTgId) {
    showError("Неверная ссылка", "Откройте тест напрямую через меню бота в Telegram.");
    return;
  }

  // Load Test Data
  fetch(`/api/test?test_id=${encodeURIComponent(testId)}&student_id=${encodeURIComponent(studentTgId)}`)
    .then((res) => {
      if (!res.ok) throw new Error("Ошибка при загрузке данных с сервера.");
      return res.json();
    })
    .then((data) => {
      if (data.error) {
        showError("Внимание", data.error);
        return;
      }
      testData = data.test;
      studentData = data.student;
      initQuiz();
    })
    .catch((err) => {
      showError("Сбой подключения", err.message || "Не удалось загрузить задания теста.");
    });

  function initQuiz() {
    elTitle.textContent = testData.title;
    elBadge.textContent = `${studentData.full_name} (${studentData.class_name})`;

    renderQuestions();
    updateProgress();

    elLoading.classList.add("hidden");
    elQuizContainer.classList.remove("hidden");
    elFooter.classList.remove("hidden");
  }

  function renderQuestions() {
    elQuizContainer.innerHTML = "";

    testData.questions.forEach((q, idx) => {
      const qNum = idx + 1;
      const card = document.createElement("div");
      card.className = "question-card";
      card.id = `q-card-${q.id}`;

      let html = "";

      // Reading Passage Context (if any)
      if (q.context_text && q.context_text.trim()) {
        html += `
          <div class="reading-box">
            <span class="reading-label">Текст к заданию</span>
            ${escapeHtml(q.context_text)}
          </div>
        `;
      }

      html += `
        <div class="question-header">
          <span class="question-num">Вопрос ${qNum} из ${testData.questions.length}</span>
        </div>
        <div class="question-text">${escapeHtml(q.question_text)}</div>
      `;

      // Multiple choice with custom radio buttons
      if (q.options && q.options.length > 0) {
        html += `<div class="options-list">`;
        q.options.forEach((opt, optIdx) => {
          html += `
            <label class="option-card" data-qid="${q.id}" data-val="${escapeHtml(opt)}">
              <input type="radio" name="question_${q.id}" value="${escapeHtml(opt)}">
              <span class="custom-radio"></span>
              <span class="option-text">${escapeHtml(opt)}</span>
            </label>
          `;
        });
        html += `</div>`;
      } else {
        // Open Cloze / Word Formation text input
        html += `
          <div class="custom-input-wrapper">
            <input type="text" class="custom-text-input" data-qid="${q.id}" placeholder="Напишите ответ словом..." autocomplete="off">
          </div>
        `;
      }

      card.innerHTML = html;
      elQuizContainer.appendChild(card);
    });

    // Attach Event Listeners to Options
    elQuizContainer.querySelectorAll(".option-card").forEach((optLabel) => {
      optLabel.addEventListener("click", function (e) {
        const qid = this.getAttribute("data-qid");
        const val = this.getAttribute("data-val");

        // Clear sibling selected states
        const parentList = this.closest(".options-list");
        parentList.querySelectorAll(".option-card").forEach((c) => c.classList.remove("selected"));

        this.classList.add("selected");
        const radio = this.querySelector('input[type="radio"]');
        if (radio) radio.checked = true;

        userAnswers[qid] = val;
        markCardAnswered(qid, true);
        updateProgress();
      });
    });

    // Attach Event Listeners to Text Inputs
    elQuizContainer.querySelectorAll(".custom-text-input").forEach((input) => {
      input.addEventListener("input", function () {
        const qid = this.getAttribute("data-qid");
        const val = this.value.trim();

        if (val) {
          userAnswers[qid] = val;
          markCardAnswered(qid, true);
        } else {
          delete userAnswers[qid];
          markCardAnswered(qid, false);
        }
        updateProgress();
      });
    });
  }

  function markCardAnswered(qid, isAnswered) {
    const card = document.getElementById(`q-card-${qid}`);
    if (card) {
      if (isAnswered) card.classList.add("answered");
      else card.classList.remove("answered");
    }
  }

  function updateProgress() {
    const total = testData.questions.length;
    const answeredCount = Object.keys(userAnswers).length;
    const pct = total > 0 ? (answeredCount / total) * 100 : 0;

    elProgressBar.style.width = `${pct}%`;
    elAnsweredCounter.textContent = `Отвечено: ${answeredCount} из ${total}`;

    // Enable submit if at least 1 answer provided (or all, but allowing submit even if some skipped)
    elSubmitBtn.disabled = answeredCount === 0;
  }

  // Submit flow
  elSubmitBtn.addEventListener("click", () => {
    const total = testData.questions.length;
    const answeredCount = Object.keys(userAnswers).length;
    const unanswered = total - answeredCount;

    if (unanswered > 0) {
      elModalDesc.textContent = `Вы ответили на ${answeredCount} из ${total} вопросов. ${unanswered} заданий остались без ответа. Отправить работу?`;
    } else {
      elModalDesc.textContent = `Вы ответили на все ${total} заданий. Отправить работу учителю на проверку?`;
    }

    elConfirmModal.classList.remove("hidden");
  });

  elModalCancel.addEventListener("click", () => {
    elConfirmModal.classList.add("hidden");
  });

  elModalConfirm.addEventListener("click", () => {
    elConfirmModal.classList.add("hidden");
    sendSubmission();
  });

  function sendSubmission() {
    elSubmitBtn.disabled = true;
    elSubmitBtn.textContent = "Отправка...";

    const elapsed = Math.round((Date.now() - startTime) / 1000);

    const payload = {
      test_id: testData.id,
      student_id: studentData.id,
      answers: userAnswers,
      time_spent_seconds: elapsed,
    };

    fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((res) => {
        if (!res.ok) throw new Error("Не удалось сохранить ответы.");
        return res.json();
      })
      .then((data) => {
        if (data.error) throw new Error(data.error);

        // Success: Show clean success screen with NO SCORES
        elSuccessScreen.classList.remove("hidden");
        if (tg?.HapticFeedback) {
          tg.HapticFeedback.notificationOccurred("success");
        }
      })
      .catch((err) => {
        alert("Ошибка при отправке: " + err.message);
        elSubmitBtn.disabled = false;
        elSubmitBtn.textContent = "Отправить работу";
      });
  }

  elCloseBtn.addEventListener("click", () => {
    if (tg) {
      tg.close();
    } else {
      window.close();
    }
  });

  function showError(title, desc) {
    elLoading.classList.add("hidden");
    elQuizContainer.classList.add("hidden");
    elFooter.classList.add("hidden");
    elErrorScreen.classList.remove("hidden");
    elErrorTitle.textContent = title;
    elErrorDesc.textContent = desc;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
});
