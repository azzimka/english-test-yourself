// --- Telegram Mini App Engine for Teacher Dashboard ---
document.addEventListener("DOMContentLoaded", () => {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.expand();
    tg.ready();
    if (tg.setHeaderColor) tg.setHeaderColor("#FFFFFF");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#FFFFFF");
  }

  // Parse URL search params & Telegram user ID with multi-layer fallback
  const urlParams = new URLSearchParams(window.location.search);
  let teacherId = urlParams.get("teacher_id") || (tg?.initDataUnsafe?.user?.id ? String(tg.initDataUnsafe.user.id) : null);

  // Fallback 1: Extract user ID from raw tg.initData if initDataUnsafe was omitted
  if (!teacherId && tg?.initData) {
    try {
      const q = new URLSearchParams(tg.initData);
      const uStr = q.get("user");
      if (uStr) {
        const uObj = JSON.parse(uStr);
        if (uObj?.id) teacherId = String(uObj.id);
      }
    } catch (e) {}
  }

  // Fallback 2: Retrieve cached teacher_id from localStorage / sessionStorage
  if (!teacherId) {
    try {
      teacherId = sessionStorage.getItem("teacher_id") || localStorage.getItem("teacher_id");
    } catch (e) {}
  }

  // Cache teacherId whenever detected so subsequent tabs or refreshes work seamlessly
  if (teacherId) {
    try {
      sessionStorage.setItem("teacher_id", teacherId);
      localStorage.setItem("teacher_id", teacherId);
    } catch (e) {}
  }

  // DOM Elements
  const elLoading = document.getElementById("loading");
  const elErrorScreen = document.getElementById("error-screen");
  const elErrorTitle = document.getElementById("error-title");
  const elErrorDesc = document.getElementById("error-desc");
  const elRetryBtn = document.getElementById("retry-btn");

  const elPageTitle = document.getElementById("page-title");
  const elPageSubtitle = document.getElementById("page-subtitle");
  const elBackBtn = document.getElementById("back-btn");

  const elViewTests = document.getElementById("view-tests");
  const elTestsCount = document.getElementById("tests-count");
  const elTestsList = document.getElementById("tests-list");

  const elViewSubmissions = document.getElementById("view-submissions");
  const elActiveTestTitle = document.getElementById("active-test-title");
  const elBannerCategory = document.getElementById("banner-category");
  const elBannerStats = document.getElementById("banner-stats");
  const elConfirmAllBtn = document.getElementById("confirm-all-btn");

  const elCountAll = document.getElementById("count-all");
  const elCountPending = document.getElementById("count-pending");
  const elCountConfirmed = document.getElementById("count-confirmed");
  const elSubmissionsList = document.getElementById("submissions-list");

  const elToast = document.getElementById("toast");
  const elToastMsg = document.getElementById("toast-message");

  // State
  let testsData = [];
  let currentTest = null;
  let currentSubmissions = [];
  let currentFilter = "all"; // 'all' | 'pending' | 'confirmed'

  if (!teacherId) {
    showError(
      "Доступ ограничен",
      "Пожалуйста, отправьте команду /start в чате бота и откройте ведомость через меню."
    );
    if (elRetryBtn && tg?.close) {
      elRetryBtn.textContent = "Вернуться в бот";
      elRetryBtn.onclick = () => tg.close();
    }
    return;
  }

  // Initial Load
  loadTests();

  elRetryBtn.addEventListener("click", () => {
    if (currentTest) openTestSubmissions(currentTest.id);
    else loadTests();
  });

  elBackBtn.addEventListener("click", () => {
    showTestsView();
  });

  // Filter Chips
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", function () {
      document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      this.classList.add("active");
      currentFilter = this.getAttribute("data-filter");
      renderSubmissions();
    });
  });

  // Confirm All Button
  elConfirmAllBtn.addEventListener("click", () => {
    if (!currentTest) return;
    const pendingSubs = currentSubmissions.filter((s) => s.status === "pending");
    if (pendingSubs.length === 0) return;

    if (!confirm(`Утвердить все ${pendingSubs.length} ожидающих работ? Ученики получат уведомления со своими баллами.`)) {
      return;
    }

    elConfirmAllBtn.disabled = true;
    elConfirmAllBtn.textContent = "Утверждаем...";

    fetch("/api/teacher/confirm-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        teacher_id: teacherId,
        test_id: currentTest.id,
      }),
    })
      .then((res) => res.json())
      .then((data) => {
        elConfirmAllBtn.disabled = false;
        if (data.error) throw new Error(data.error);

        // Update local state
        currentSubmissions.forEach((s) => {
          s.status = "confirmed";
        });

        triggerHaptic();
        showToast(`Утверждено ${data.confirmed_count} работ! Ученики уведомлены.`);
        updateCountsAndBanner();
        renderSubmissions();
      })
      .catch((err) => {
        elConfirmAllBtn.disabled = false;
        alert("Ошибка: " + err.message);
      });
  });

  function loadTests() {
    showLoading();
    fetch(`/api/teacher/tests?teacher_id=${encodeURIComponent(teacherId)}`)
      .then((res) => {
        if (res.status === 403) throw new Error("У вас нет прав администратора в этом боте.");
        if (!res.ok) throw new Error("Ошибка при загрузке тестов с сервера.");
        return res.json();
      })
      .then((data) => {
        if (data.error) throw new Error(data.error);
        testsData = data.tests || [];
        renderTestsList();
        showTestsView();
      })
      .catch((err) => {
        showError("Сбой загрузки", err.message);
      });
  }

  function renderTestsList() {
    elTestsCount.textContent = `Всего: ${testsData.length}`;
    elTestsList.innerHTML = "";

    if (testsData.length === 0) {
      elTestsList.innerHTML = `
        <div class="state-container">
          <p class="state-text">Вы еще не создали ни одного теста.<br>Нажмите «📝 Создать тест» в меню бота.</p>
        </div>
      `;
      return;
    }

    testsData.forEach((t) => {
      const card = document.createElement("div");
      card.className = "test-card";

      const typeLabel = t.test_type === "olympiad" ? "🏆 Олимпиадный" : "📚 Обычный";
      const pendingCount = t.pending_count || 0;
      const confirmedCount = t.confirmed_count || 0;
      const total = t.total_submissions || 0;

      let badgesHtml = `<span class="tag-type">${typeLabel}</span>`;
      badgesHtml += `<span class="meta-text" style="font-size:12px;">Сдач: ${total}</span>`;

      if (pendingCount > 0) {
        badgesHtml += `<span class="tag-pending">⏳ ${pendingCount} на проверке</span>`;
      }
      if (confirmedCount > 0) {
        badgesHtml += `<span class="tag-confirmed">✅ ${confirmedCount} проверено</span>`;
      }

      card.innerHTML = `
        <div class="test-card-content">
          <div class="test-card-title">${escapeHtml(t.title)}</div>
          <div class="test-card-badges">${badgesHtml}</div>
        </div>
        <div class="arrow-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="9 18 15 12 9 6"></polyline>
          </svg>
        </div>
      `;

      card.addEventListener("click", () => {
        openTestSubmissions(t.id);
      });

      elTestsList.appendChild(card);
    });
  }

  function openTestSubmissions(testId) {
    showLoading();
    fetch(`/api/teacher/submissions?test_id=${encodeURIComponent(testId)}&teacher_id=${encodeURIComponent(teacherId)}`)
      .then((res) => {
        if (!res.ok) throw new Error("Не удалось загрузить работы по тесту.");
        return res.json();
      })
      .then((data) => {
        if (data.error) throw new Error(data.error);
        currentTest = data.test;
        currentSubmissions = data.submissions || [];

        showSubmissionsView();
        updateCountsAndBanner();
        renderSubmissions();
      })
      .catch((err) => {
        showError("Ошибка", err.message);
      });
  }

  function updateCountsAndBanner() {
    if (elActiveTestTitle && currentTest) elActiveTestTitle.textContent = currentTest.title;
    if (elBannerCategory && currentTest) {
      elBannerCategory.textContent = currentTest.test_type === "olympiad" ? "🏆 Олимпиадный" : "📚 Обычный";
    }

    const total = currentSubmissions.length;
    const pending = currentSubmissions.filter((s) => s.status === "pending").length;
    const confirmed = currentSubmissions.filter((s) => s.status === "confirmed").length;

    if (elBannerStats) elBannerStats.textContent = `${total} сданных работ`;
    elCountAll.textContent = total;
    elCountPending.textContent = pending;
    elCountConfirmed.textContent = confirmed;

    if (pending > 0) {
      elConfirmAllBtn.classList.remove("hidden");
      elConfirmAllBtn.innerHTML = `<span>✅ Утвердить все (${pending})</span>`;
    } else {
      elConfirmAllBtn.classList.add("hidden");
    }
  }

  function renderSubmissions() {
    elSubmissionsList.innerHTML = "";

    let filtered = currentSubmissions;
    if (currentFilter === "pending") {
      filtered = currentSubmissions.filter((s) => s.status === "pending");
    } else if (currentFilter === "confirmed") {
      filtered = currentSubmissions.filter((s) => s.status === "confirmed");
    }

    if (filtered.length === 0) {
      elSubmissionsList.innerHTML = `
        <div class="state-container" style="padding: 40px 20px;">
          <p class="state-text">В этой категории пока нет работ.</p>
        </div>
      `;
      return;
    }

    filtered.forEach((sub) => {
      const card = document.createElement("div");
      card.className = "submission-card";
      card.id = `sub-card-${sub.id}`;

      const totalQ = sub.total_questions || (sub.answers ? sub.answers.length : 1);
      const isPending = sub.status === "pending";
      const gradedTotal = sub.answers
        ? sub.answers.filter((x) => x.is_correct === true || x.is_correct === false).length
        : 0;

      const pct = Math.round((sub.score / totalQ) * 100);
      const statusBadge = isPending
        ? `<span class="status-badge-pending">⏳ На проверке</span>`
        : `<span class="status-badge-confirmed">✅ Утверждено</span>`;

      let scoreBadgeHtml = "";
      if (isPending) {
        if (gradedTotal === 0) {
          scoreBadgeHtml = `<span class="score-unrated">Ожидает проверки</span>`;
        } else {
          scoreBadgeHtml = `<span class="score-grading">${sub.score}/${totalQ} (${pct}%)</span>`;
        }
      } else {
        scoreBadgeHtml = `<span class="score-confirmed">${sub.score}/${totalQ} (${pct}%)</span>`;
      }

      const minutes = Math.floor((sub.time_spent_seconds || 0) / 60);
      const seconds = (sub.time_spent_seconds || 0) % 60;
      const timeStr = `${minutes} мин. ${seconds} сек.`;

      // Progress Tracker for pending submissions
      let progressHtml = "";
      if (isPending && sub.answers && sub.answers.length > 0) {
        const progressPct = Math.round((gradedTotal / totalQ) * 100);
        progressHtml = `
          <div class="grading-progress-box">
            <div class="grading-progress-label">
              <span>Проверено заданий:</span>
              <span class="progress-nums"><strong>${gradedTotal}</strong> из ${totalQ}</span>
            </div>
            <div class="grading-progress-track">
              <div class="grading-progress-fill" style="width: ${progressPct}%;"></div>
            </div>
          </div>
        `;
      }

      // Build Questions & Answers HTML
      let answersHtml = "";
      if (sub.answers && sub.answers.length > 0) {
        answersHtml += `<div class="answers-items">`;
        sub.answers.forEach((a, idx) => {
          const qNum = idx + 1;
          const isCorrect = a.is_correct === true;
          const isIncorrect = a.is_correct === false;
          const imageHtml = a.image_url
            ? `<div class="teacher-q-img-wrap"><img src="${escapeHtml(a.image_url)}" class="teacher-q-img" alt="Diagram" /></div>`
            : "";

          let ansDisplay = "";
          let userAns = a.user_answer;
          if (typeof userAns === "string" && userAns.trim().startsWith("[") && userAns.trim().endsWith("]")) {
            try {
              const parsed = JSON.parse(userAns);
              if (Array.isArray(parsed)) userAns = parsed;
            } catch (e) {}
          }

          if (Array.isArray(userAns)) {
            const nonEmpty = userAns.filter((item) => item && String(item).trim() && String(item).trim() !== "(Без ответа)");
            if (nonEmpty.length > 0) {
              ansDisplay = `<div class="teacher-student-ans-tags">` +
                nonEmpty.map((val) => `<span class="teacher-ans-tag">${escapeHtml(val)}</span>`).join("") +
                `</div>`;
            } else {
              ansDisplay = `<span class="teacher-student-ans-val empty">(Без ответа)</span>`;
            }
          } else {
            const strVal = userAns != null ? String(userAns).trim() : "";
            const hasAns = strVal && strVal !== "(Без ответа)";
            ansDisplay = hasAns
              ? `<span class="teacher-student-ans-val">${escapeHtml(strVal)}</span>`
              : `<span class="teacher-student-ans-val empty">(Без ответа)</span>`;
          }

          const cardStateClass = isCorrect ? "card-correct" : isIncorrect ? "card-incorrect" : "";

          answersHtml += `
            <div class="teacher-q-card ${cardStateClass}" data-sub-id="${sub.id}" data-q-idx="${idx}">
              <div class="teacher-q-top">
                <span class="teacher-q-badge">№${qNum}</span>
                <div class="teacher-q-text">${escapeHtml(a.question)}</div>
              </div>
              ${imageHtml}
              <div class="teacher-student-ans-row">
                <div class="teacher-student-ans-left">
                  <span class="ans-label-dot"></span>
                  <span class="teacher-student-ans-label">Ответ ученика</span>
                </div>
                ${ansDisplay}
              </div>
              <div class="teacher-grade-actions">
                <button type="button" class="btn-grade btn-grade-correct ${isCorrect ? "active" : ""}" data-action="correct" data-sub-id="${sub.id}" data-q-idx="${idx}" aria-label="Верно">
                  <span class="grade-icon-circle">
                    <svg class="grade-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                      <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                  </span>
                  <span class="grade-text">Верно</span>
                </button>
                <button type="button" class="btn-grade btn-grade-incorrect ${isIncorrect ? "active" : ""}" data-action="incorrect" data-sub-id="${sub.id}" data-q-idx="${idx}" aria-label="Неверно">
                  <span class="grade-icon-circle">
                    <svg class="grade-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                      <line x1="18" y1="6" x2="6" y2="18"></line>
                      <line x1="6" y1="6" x2="18" y2="18"></line>
                    </svg>
                  </span>
                  <span class="grade-text">Неверно</span>
                </button>
              </div>
            </div>
          `;
        });
        answersHtml += `</div>`;
      } else {
        answersHtml = `<p class="state-text">Детали ответов отсутствуют.</p>`;
      }

      // Action Button
      let actionBtnHtml = "";
      if (isPending) {
        actionBtnHtml = `
          <button class="btn-confirm-single" data-sub-id="${sub.id}">
            Подтвердить оценку (${sub.score}/${totalQ})
          </button>
        `;
      } else {
        actionBtnHtml = `
          <div class="confirmed-status-wrap">
            <div class="btn-confirmed-static">
              <span>✓ Оценка утверждена (${sub.score}/${totalQ})</span>
            </div>
            <button class="btn-save-regrade hidden" data-sub-id="${sub.id}">
              Сохранить изменения (${sub.score}/${totalQ})
            </button>
          </div>
        `;
      }

      card.innerHTML = `
        <div class="card-header-clickable">
          <div class="student-primary">
            <div class="student-name">${escapeHtml(sub.full_name || "Ученик")}</div>
            <div class="student-class">${escapeHtml(sub.class_name || "Класс не указан")} • ⏱ ${timeStr}</div>
          </div>
          <div class="submission-stats">
            <div class="score-badge">${scoreBadgeHtml}</div>
            ${statusBadge}
          </div>
          <div class="accordion-arrow">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
          </div>
        </div>

        <div class="card-details">
          ${progressHtml}
          <div class="answers-title">Ответы ученика по заданиям:</div>
          ${answersHtml}
          <div class="card-actions-bar">
            ${actionBtnHtml}
          </div>
        </div>
      `;

      // Accordion toggle on header click
      const headerClickable = card.querySelector(".card-header-clickable");
      headerClickable.addEventListener("click", () => {
        card.classList.toggle("expanded");
      });

      // Grade buttons event handling for this card
      card.querySelectorAll(".btn-grade").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const qIdx = parseInt(btn.getAttribute("data-q-idx"), 10);
          const isCorrectChoice = btn.classList.contains("btn-grade-correct");
          const wasActive = btn.classList.contains("active");

          const qCard = btn.closest(".teacher-q-card");
          const btnCorr = qCard.querySelector(".btn-grade-correct");
          const btnIncorr = qCard.querySelector(".btn-grade-incorrect");

          if (wasActive) {
            // Tapping an already selected button untoggles it!
            btn.classList.remove("active");
            qCard.classList.remove("card-correct", "card-incorrect");
            if (sub.answers && sub.answers[qIdx]) {
              sub.answers[qIdx].is_correct = null;
            }
          } else {
            // Select choice
            if (isCorrectChoice) {
              btnCorr.classList.add("active");
              btnIncorr.classList.remove("active");
              qCard.classList.add("card-correct");
              qCard.classList.remove("card-incorrect");
              if (sub.answers && sub.answers[qIdx]) {
                sub.answers[qIdx].is_correct = true;
              }
            } else {
              btnIncorr.classList.add("active");
              btnCorr.classList.remove("active");
              qCard.classList.add("card-incorrect");
              qCard.classList.remove("card-correct");
              if (sub.answers && sub.answers[qIdx]) {
                sub.answers[qIdx].is_correct = false;
              }
            }
          }

          // Trigger light tactile feedback
          triggerHapticLight();

          // Dynamically recalculate score for this submission
          const currentScore = sub.answers.filter((x) => x.is_correct === true).length;
          const currentGradedTotal = sub.answers.filter((x) => x.is_correct === true || x.is_correct === false).length;
          sub.score = currentScore;
          const newPct = Math.round((currentScore / totalQ) * 100);

          // Update header score badge
          const scoreBadge = card.querySelector(".score-badge");
          if (scoreBadge) {
            if (sub.status === "pending" && currentGradedTotal === 0) {
              scoreBadge.innerHTML = `<span class="score-unrated">Ожидает проверки</span>`;
            } else {
              scoreBadge.innerHTML = `<span class="score-grading">${currentScore}/${totalQ} (${newPct}%)</span>`;
            }
          }

          // Update progress box if present
          const progressNums = card.querySelector(".progress-nums");
          if (progressNums) {
            progressNums.innerHTML = `<strong>${currentGradedTotal}</strong> из ${totalQ}`;
          }
          const progressFill = card.querySelector(".grading-progress-fill");
          if (progressFill) {
            progressFill.style.width = `${Math.round((currentGradedTotal / totalQ) * 100)}%`;
          }

          // Update action buttons text & reveal regrade save button
          const confirmBtn = card.querySelector(".btn-confirm-single");
          if (confirmBtn) {
            confirmBtn.textContent = `Подтвердить оценку (${currentScore}/${totalQ})`;
          }

          const regradeBtn = card.querySelector(".btn-save-regrade");
          if (regradeBtn) {
            regradeBtn.classList.remove("hidden");
            regradeBtn.textContent = `Сохранить изменения (${currentScore}/${totalQ})`;
          }
        });
      });

      // Confirm Single Button
      const confirmBtn = card.querySelector(".btn-confirm-single");
      if (confirmBtn) {
        confirmBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          confirmSingleSubmission(sub.id, confirmBtn);
        });
      }

      // Regrade Save Button
      const regradeBtn = card.querySelector(".btn-save-regrade");
      if (regradeBtn) {
        regradeBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          confirmSingleSubmission(sub.id, regradeBtn);
        });
      }

      elSubmissionsList.appendChild(card);
    });
  }

  function confirmSingleSubmission(subId, btnEl) {
    const sub = currentSubmissions.find((s) => s.id === subId);
    if (!sub) return;

    const totalQ = sub.total_questions || (sub.answers ? sub.answers.length : 1);
    const gradedTotal = sub.answers
      ? sub.answers.filter((x) => x.is_correct === true || x.is_correct === false).length
      : 0;
    const unrated = totalQ - gradedTotal;

    if (unrated > 0) {
      const confirmMsg = `Вы проверили ${gradedTotal} из ${totalQ} заданий.\nОставшиеся ${unrated} будут засчитаны как неверные (0 баллов).\n\nПодтвердить оценку ${sub.score}/${totalQ}?`;
      if (!confirm(confirmMsg)) {
        return;
      }
      sub.answers.forEach((a) => {
        if (a.is_correct === null || a.is_correct === undefined) {
          a.is_correct = false;
        }
      });
    }

    btnEl.disabled = true;
    const originalText = btnEl.textContent;
    btnEl.textContent = "Сохраняем...";

    fetch("/api/teacher/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        teacher_id: teacherId,
        submission_id: subId,
        score: sub.score,
        answers: sub.answers,
      }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.error) throw new Error(data.error);

        // Update local object
        sub.status = "confirmed";
        if (data.submission && typeof data.submission.score === "number") {
          sub.score = data.submission.score;
        }

        triggerHapticSuccess();
        showToast(`Оценка (${sub.score}/${totalQ}) подтверждена! Ученик получил уведомление.`);
        updateCountsAndBanner();
        renderSubmissions();
      })
      .catch((err) => {
        btnEl.disabled = false;
        btnEl.textContent = originalText;
        alert("Ошибка: " + err.message);
      });
  }

  // Navigation helpers
  function showTestsView() {
    elBackBtn.classList.add("hidden");
    elPageTitle.textContent = "Ведомость учителя";
    elPageSubtitle.textContent = "Результаты и проверка работ";

    elLoading.classList.add("hidden");
    elErrorScreen.classList.add("hidden");
    elViewSubmissions.classList.add("hidden");
    elViewTests.classList.remove("hidden");
  }

  function showSubmissionsView() {
    elBackBtn.classList.remove("hidden");
    elPageTitle.textContent = currentTest.title;
    const typeLabel = currentTest.test_type === "olympiad" ? "🏆 Олимпиадный тест" : "📚 Обычный тест";
    elPageSubtitle.textContent = `${typeLabel} • Проверка сданных работ`;

    elLoading.classList.add("hidden");
    elErrorScreen.classList.add("hidden");
    elViewTests.classList.add("hidden");
    elViewSubmissions.classList.remove("hidden");
  }

  function showLoading() {
    elLoading.classList.remove("hidden");
    elErrorScreen.classList.add("hidden");
    elViewTests.classList.add("hidden");
    elViewSubmissions.classList.add("hidden");
  }

  function showError(title, desc) {
    elLoading.classList.add("hidden");
    elViewTests.classList.add("hidden");
    elViewSubmissions.classList.add("hidden");
    elErrorScreen.classList.remove("hidden");
    elErrorTitle.textContent = title;
    elErrorDesc.textContent = desc;
  }

  function showToast(text) {
    elToastMsg.textContent = text;
    elToast.classList.remove("hidden");
    setTimeout(() => {
      elToast.classList.add("hidden");
    }, 3000);
  }

  function triggerHaptic() {
    triggerHapticSuccess();
  }

  function triggerHapticSuccess() {
    if (tg?.HapticFeedback) {
      tg.HapticFeedback.notificationOccurred("success");
    }
  }

  function triggerHapticLight() {
    if (tg?.HapticFeedback) {
      tg.HapticFeedback.impactOccurred("light");
    }
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
