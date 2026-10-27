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
    elActiveTestTitle.textContent = currentTest.title;
    elBannerCategory.textContent = currentTest.test_type === "olympiad" ? "🏆 Олимпиадный" : "📚 Обычный";

    const total = currentSubmissions.length;
    const pending = currentSubmissions.filter((s) => s.status === "pending").length;
    const confirmed = currentSubmissions.filter((s) => s.status === "confirmed").length;

    elBannerStats.textContent = `${total} сданных работ`;
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

      const pct = Math.round((sub.score / (sub.total_questions || 1)) * 100);
      const isPending = sub.status === "pending";
      const statusBadge = isPending
        ? `<span class="status-badge-pending">⏳ На проверке</span>`
        : `<span class="status-badge-confirmed">✅ Утверждено</span>`;

      const minutes = Math.floor((sub.time_spent_seconds || 0) / 60);
      const seconds = (sub.time_spent_seconds || 0) % 60;
      const timeStr = `${minutes} мин. ${seconds} сек.`;

      // Build Questions & Answers HTML
      let answersHtml = "";
      if (sub.answers && sub.answers.length > 0) {
        answersHtml += `<div class="answers-items">`;
        sub.answers.forEach((a, idx) => {
          const qNum = idx + 1;
          const statusIcon = a.is_correct ? "✅" : "❌";
          const explanationHtml = a.explanation ? `<div class="explanation-box">Правило: ${escapeHtml(a.explanation)}</div>` : "";

          answersHtml += `
            <div class="answer-row">
              <div class="answer-q-title">${qNum}. ${escapeHtml(a.question)}</div>
              <div class="answer-choices">
                <div class="choice-line">
                  <span class="choice-label">Ответ ученика:</span>
                  <span class="val-user">${escapeHtml(a.user_answer || "—")}</span>
                  <span class="is-correct-icon">${statusIcon}</span>
                </div>
                <div class="choice-line">
                  <span class="choice-label">Правильно:</span>
                  <span class="val-correct">${escapeHtml(a.correct_answer || "—")}</span>
                </div>
              </div>
              ${explanationHtml}
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
            Подтвердить оценку
          </button>
        `;
      } else {
        actionBtnHtml = `
          <div class="btn-confirmed-static">
            <span>✓ Оценка утверждена ученику</span>
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
            <div class="score-badge">${sub.score}/${sub.total_questions} (${pct}%)</div>
            ${statusBadge}
          </div>
        </div>

        <div class="card-details">
          <div class="answers-title">Ответы ученика по вопросам:</div>
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

      // Confirm Single Button
      const confirmBtn = card.querySelector(".btn-confirm-single");
      if (confirmBtn) {
        confirmBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          confirmSingleSubmission(sub.id, confirmBtn);
        });
      }

      elSubmissionsList.appendChild(card);
    });
  }

  function confirmSingleSubmission(subId, btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "Сохраняем...";

    fetch("/api/teacher/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        teacher_id: teacherId,
        submission_id: subId,
      }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.error) throw new Error(data.error);

        // Update local object
        const sub = currentSubmissions.find((s) => s.id === subId);
        if (sub) {
          sub.status = "confirmed";
        }

        triggerHaptic();
        showToast("Оценка подтверждена! Ученик получил уведомление.");
        updateCountsAndBanner();
        renderSubmissions();
      })
      .catch((err) => {
        btnEl.disabled = false;
        btnEl.textContent = "Подтвердить оценку";
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
    elPageSubtitle.textContent = "Проверка сданных работ";

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
    if (tg?.HapticFeedback) {
      tg.HapticFeedback.notificationOccurred("success");
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
