(() => {
  "use strict";

  const chapters = window.curriculum.chapters;
  const $ = (selector) => document.querySelector(selector);
  const chapterGrid = $("#chapterGrid");
  const dialog = $("#authDialog");
  const welcomePhoto = $("#welcomePhoto");
  const defaultWelcomePhoto = welcomePhoto.getAttribute("src");
  let filterStarred = false;
  let currentUser = null;
  let exercises = [];
  let learningCatalog = { chapters: [], lessons: [] };
  let learningState = { lessons: [], attempts: [], errors: [] };
  let activeLessonId = null;
  let selectedLearningChapter = null;
  let dailyQuiz = null;
  let dailyResult = null;
  let eeshMaterials = [];
  let activeEeshMaterial = null;
  let eeshMaterialResponses = {};
  let activeEeshSlide = 1;
  const learningQuizResults = new Map();
  const chapterGames = new Map();
  let assessmentTimer;
  let eeshQuizTimer;
  let toastTimer;
  let startedAt;
  let newJoinCode = "";

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers
      }
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "Хүсэлтийг гүйцэтгэж чадсангүй.");
    return result;
  }

  async function post(path, body = {}) {
    return api(path, { method: "POST", body: JSON.stringify(body) });
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("show"), 3200);
  }

  function applyTheme(theme) {
    const isDark = theme === "dark";
    document.body.classList.toggle("dark-mode", isDark);
    const button = $("#themeToggle");
    button.setAttribute("aria-label", isDark ? "Цайвар горим асаах" : "Харанхуй горим асаах");
    button.title = isDark ? "Цайвар горим" : "Харанхуй горим";
    button.innerHTML = `<i data-lucide="${isDark ? "sun" : "moon"}" aria-hidden="true"></i>`;
    if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
  }

  function requireLogin() {
    if (currentUser) return true;
    openAuth();
    showToast("Энэ үйлдлийг хийхийн тулд бүртгүүлж эсвэл нэвтэрнэ үү.");
    return false;
  }

  function requireStudent() {
    if (currentUser?.role === "student") return true;
    if (currentUser) showToast("Энэ сорилыг сурагчийн эрхээр ажиллуулна.");
    else requireLogin();
    return false;
  }

  function renderStats() {
    $("#chapterCount").textContent = chapters.length;
    $("#topicCount").textContent = chapters.reduce((sum, chapter) => sum + chapter.topics.length, 0);
    $("#criteriaCount").textContent = chapters.reduce((sum, chapter) => sum + chapter.criteria.length, 0);
    $("#starCount").textContent = chapters.reduce((sum, chapter) => sum + chapter.objectives.filter((item) => item.code.includes("*")).length, 0);
    $("#criterionSummary").textContent = `${$("#criteriaCount").textContent} шалгуур · ${chapters.length} бүлэг`;
  }

  function renderObjective(objective) {
    const starred = objective.code.includes("*");
    return `<div class="objective-row"><span class="objective-code${starred ? " starred" : ""}">${escapeHtml(objective.code.replace("*", ""))}${starred ? " ✦" : ""}</span><p>${escapeHtml(objective.text)}</p></div>`;
  }

  const chapterGameTitles = {
    absolute: "Модулийн марафон",
    polynomials: "Олон гишүүнтийн код тайлагч",
    functions: "Функцийн эрэлд",
    derivatives: "Уламжлалын хурдны сорил",
    integrals: "Интегралын эрдэнэс",
    probability: "Магадлалын сорил",
    complex: "Комплекс тооны портал"
  };

  function chapterGameMarkup(chapter, game) {
    const title = chapterGameTitles[chapter.id] || `${chapter.title} · сорил`;
    if (!game) {
      return `<div class="chapter-game-card"><div class="chapter-game-intro"><span class="chapter-game-icon">🎮</span><div><strong>${escapeHtml(title)}</strong><span>3 бодлого · зөв хариултад оноо, хурдан бодвол бонус</span></div></div><button class="filter-button chapter-game-start" type="button" data-start-chapter-game="${escapeHtml(chapter.id)}">Тоглож эхлэх <span aria-hidden="true">→</span></button><div class="chapter-game-area" id="chapter-game-${escapeHtml(chapter.id)}" aria-live="polite"></div></div>`;
    }

    if (game.completed) {
      return `<div class="chapter-game-card"><div class="chapter-game-intro"><span class="chapter-game-icon">🏆</span><div><strong>${escapeHtml(title)}</strong><span>${game.correctCount}/3 зөв · нийт ${game.score} оноо</span></div></div><button class="filter-button chapter-game-start" type="button" data-start-chapter-game="${escapeHtml(chapter.id)}">Дахин тоглох ↻</button></div>`;
    }

    const exercise = game.questions[game.index];
    const feedback = game.feedback
      ? `<div class="chapter-game-feedback ${game.feedback.correct ? "correct" : "incorrect"}">${game.feedback.correct ? `Зөв! +${game.feedback.points_earned} оноо${game.feedback.bonus_points ? ` · хурдны бонус +${game.feedback.bonus_points}` : ""}` : "Одоохондоо оноо нэмэгдсэнгүй. Дараагийн бодлогоо оролдоорой."}</div><button class="filter-button chapter-game-next" type="button" data-next-chapter-game="${escapeHtml(chapter.id)}">${game.index === game.questions.length - 1 ? "Дүн харах" : "Дараагийн бодлого →"}</button>`
      : `<form class="chapter-game-form" data-chapter-game="${escapeHtml(chapter.id)}"><label for="game-answer-${escapeHtml(chapter.id)}">Хариулт<input id="game-answer-${escapeHtml(chapter.id)}" name="answer" required maxlength="1000" autocomplete="off"></label><button class="primary-button action-button" type="submit">Шалгах ✨</button></form>`;

    return `<div class="chapter-game-card"><div class="chapter-game-intro"><span class="chapter-game-icon">🎯</span><div><strong>${escapeHtml(title)} · ${game.index + 1}/3</strong><span>${escapeHtml(exercise.topic)} · Оноо: ${game.score}</span></div></div><div class="chapter-game-progress" role="progressbar" aria-label="Тоглоомын явц" aria-valuemin="0" aria-valuemax="3" aria-valuenow="${game.index + (game.feedback ? 1 : 0)}"><span style="width:${((game.index + (game.feedback ? 1 : 0)) / 3) * 100}%"></span></div><h4 class="chapter-game-question">${escapeHtml(exercise.prompt)}</h4><div class="chapter-game-area" id="chapter-game-${escapeHtml(chapter.id)}">${feedback}</div></div>`;
  }

  function chapterCard(chapter) {
    return `<article class="chapter-card" data-chapter="${escapeHtml(chapter.id)}">
      <button class="chapter-summary" type="button" aria-expanded="false" aria-controls="detail-${escapeHtml(chapter.id)}">
        <span class="chapter-number">${escapeHtml(chapter.number)}</span>
        <span class="chapter-main"><strong>${escapeHtml(chapter.title)}</strong><span class="chapter-meta"><span>${chapter.topics.length} дэд сэдэв</span><span>${chapter.criteria.length} шалгуур</span><span>${chapter.hours} цаг</span></span></span>
        <span class="chapter-arrow" aria-hidden="true">›</span>
      </button>
      <div class="chapter-detail" id="detail-${escapeHtml(chapter.id)}">
        <div class="detail-block"><span class="detail-label">Бүлгийн зорилго</span><p>${escapeHtml(chapter.goal)}</p></div>
        <div class="detail-block"><span class="detail-label">Дэд сэдвүүд · сурах бичиг</span><div class="topic-list">${chapter.topics.map((topic, index) => `<span class="topic-chip">${escapeHtml(chapter.number)}.${index + 1} ${escapeHtml(topic)}</span>`).join("")}</div></div>
        <div class="detail-block"><span class="detail-label">Суралцахуйн зорилт · ${chapter.objectives.length}</span><div class="objective-list">${chapter.objectives.map(renderObjective).join("")}</div></div>
        <div class="detail-block"><span class="detail-label">Суралцахуйн үр дүн</span>${chapter.outcomes.map((outcome) => `<p>${escapeHtml(outcome)}</p>`).join("")}</div>
        <div class="detail-block"><span class="detail-label">Үнэлгээний шалгуур</span><div class="criteria-pills">${chapter.criteria.map((criterion) => `<span class="criterion-pill">${escapeHtml(criterion.id)}</span>`).join("")}</div></div>
        <div class="detail-block"><span class="detail-label">Нэгжийн цагийн төлөвлөлтийн нэрс</span><div class="lesson-list">${chapter.sourceLessons.map((lesson) => `<span class="lesson-chip">${escapeHtml(lesson)}</span>`).join("")}</div></div>
        <div class="detail-block"><span class="detail-label">Тоглоомон дасгал</span>${chapterGameMarkup(chapter, chapterGames.get(chapter.id))}</div>
        <a class="chapter-progress-button" href="#exercises">Энэ платформд байгаа дасгалуудыг үзэх</a>
      </div>
    </article>`;
  }

  function renderChapterGame(chapterId, scroll = false) {
    renderChapters();
    const card = document.querySelector(`[data-chapter="${CSS.escape(chapterId)}"]`);
    if (!card) return;
    card.classList.add("expanded");
    card.querySelector(".chapter-summary").setAttribute("aria-expanded", "true");
    if (scroll) card.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function startChapterGame(chapterId) {
    if (!requireStudent()) return;
    const chapter = chapters.find((item) => item.id === chapterId);
    if (!chapter) return;
    try {
      const { exercises: chapterExercises } = await api(
        `/api/exercises?chapter_id=${encodeURIComponent(chapterId)}`
      );
      if (!chapterExercises.length) {
        showToast("Энэ бүлгийн тоглоомон дасгал одоогоор бэлэн биш байна.");
        return;
      }
      const shuffled = [...chapterExercises];
      for (let index = shuffled.length - 1; index > 0; index -= 1) {
        const randomIndex = Math.floor(Math.random() * (index + 1));
        [shuffled[index], shuffled[randomIndex]] = [shuffled[randomIndex], shuffled[index]];
      }
      const questions = [];
      const usedTopics = new Set();
      for (const exercise of shuffled) {
        if (!usedTopics.has(exercise.topic)) {
          questions.push(exercise);
          usedTopics.add(exercise.topic);
        }
        if (questions.length === 3) break;
      }
      for (const exercise of shuffled) {
        if (questions.length === 3) break;
        if (!questions.some((item) => item.id === exercise.id)) questions.push(exercise);
      }
      chapterGames.set(chapterId, {
        questions,
        index: 0,
        score: 0,
        correctCount: 0,
        startedAt: Date.now(),
        feedback: null,
        completed: false
      });
      renderChapterGame(chapterId, true);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function submitChapterGame(event) {
    event.preventDefault();
    if (!requireStudent()) return;
    const form = event.target;
    const chapterId = form.dataset.chapterGame;
    const game = chapterGames.get(chapterId);
    if (!game || game.feedback || game.completed) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const exercise = game.questions[game.index];
      const result = await post("/api/games/quiz", {
        exercise_id: exercise.id,
        answer: new FormData(form).get("answer"),
        elapsed_seconds: Math.floor((Date.now() - game.startedAt) / 1000)
      });
      game.feedback = result;
      game.score += result.points_earned;
      if (result.correct) game.correctCount += 1;
      renderChapterGame(chapterId);
      renderProgress(await api("/api/student/dashboard"));
      if ($("#leaderboard")) await refreshLeaderboard();
    } catch (error) {
      button.disabled = false;
      showToast(error.message);
    }
  }

  function advanceChapterGame(chapterId) {
    const game = chapterGames.get(chapterId);
    if (!game || !game.feedback) return;
    if (game.index === game.questions.length - 1) {
      game.completed = true;
    } else {
      game.index += 1;
      game.startedAt = Date.now();
      game.feedback = null;
    }
    renderChapterGame(chapterId, true);
  }

  function renderChapters() {
    const query = $("#searchInput").value.trim().toLocaleLowerCase("mn");
    const visible = chapters.filter((chapter) => {
      if (filterStarred && !chapter.objectives.some((objective) => objective.code.includes("*"))) return false;
      const text = [
        chapter.title, chapter.goal, ...chapter.topics,
        ...chapter.objectives.flatMap((objective) => [objective.code, objective.text]),
        ...chapter.criteria.flatMap((criterion) => [criterion.id, criterion.text])
      ].join(" ").toLocaleLowerCase("mn");
      return !query || text.includes(query);
    });
    chapterGrid.innerHTML = visible.map(chapterCard).join("");
    $("#emptyState").classList.toggle("hidden", visible.length > 0);
    if (filterStarred) {
      document.querySelectorAll(".chapter-card").forEach((card) => card.classList.add("expanded"));
      document.querySelectorAll(".chapter-summary").forEach((button) => button.setAttribute("aria-expanded", "true"));
    }
  }

  function renderCriteria() {
    $("#criteriaTable").innerHTML = chapters.flatMap((chapter) => chapter.criteria.map((criterion) => `
      <tr><td>${escapeHtml(chapter.number)} бүлэг · ${escapeHtml(chapter.title)}</td><td>${escapeHtml(criterion.id)}</td>
      <td>${escapeHtml(criterion.text)}</td><td>${criterion.objectives.map((code) => `<span class="objective-code${code.includes("*") ? " starred" : ""}">${escapeHtml(code.replace("*", ""))}${code.includes("*") ? " ✦" : ""}</span>`).join(" ")}</td></tr>`)).join("");
  }

  function renderProgress(dashboard = null) {
    const progress = new Map((dashboard?.chapters || []).map((chapter) => [chapter.id, chapter]));
    const studied = new Set((dashboard?.chapters || []).filter((chapter) => chapter.attempts > 0).map((chapter) => chapter.id));
    const percent = Math.round(studied.size / chapters.length * 100);
    $("#progressLabel").textContent = `${studied.size} / ${chapters.length} бүлэгт бодлого ажилласан`;
    $("#progressPercent").textContent = `${percent}%`;
    $("#progressBar").style.width = `${percent}%`;
    $("#progressChapters").innerHTML = chapters.map((chapter) => {
      const item = progress.get(chapter.id);
      return `<div class="progress-dot${studied.has(chapter.id) ? " done" : ""}"><strong>${escapeHtml(chapter.number)}</strong>${item?.available_exercises ? `${item.attempted_exercises}/${item.available_exercises} бодлого` : "Сан бэлдээгүй"}</div>`;
    }).join("");
    if (!dashboard) {
      $("#studentDashboard").innerHTML = `<p class="empty-state">Ахицын мэдээллийг харахын тулд сурагчийн эрхээр нэвтэрнэ үү.</p>`;
      $("#errorList").innerHTML = "";
      $("#assessmentHistory").innerHTML = "";
      return;
    }
    const summary = dashboard.summary;
    const badgeNames = {
      first_win: "Анхны зөв хариулт",
      five_wins: "5 зөв хариулт",
      score_50: "50 онооны медаль",
      score_100: "100 онооны медаль"
    };
    $("#studentDashboard").innerHTML = `
      <div class="student-metric"><span>Зөв хариултын хувь</span><strong>${summary.accuracy_percent}%</strong></div>
      <div class="student-metric"><span>Дасгалын оноо</span><strong>${summary.exercise_points}</strong></div>
      <div class="student-metric"><span>Тоглоомын оноо</span><strong>${summary.game_points}</strong></div>
      <div class="student-metric"><span>Түвшин · дараагийнх хүртэл</span><strong>${summary.level} · ${summary.points_to_next_level} оноо</strong></div>
      <div class="badge-list">${dashboard.badges.length ? dashboard.badges.map((badge) => `<span class="badge-pill">🏅 ${escapeHtml(badgeNames[badge.badge_code] || badge.badge_code)}</span>`).join("") : `<span class="empty-state">Одоогоор медаль аваагүй.</span>`}</div>`;
    $("#errorList").innerHTML = `<h3>Дахин ажиллах буруу хариултууд · ${dashboard.errors.length}</h3>${dashboard.errors.length ? dashboard.errors.map((error) =>
      `<div class="error-item"><strong>${escapeHtml(error.topic)} · ${escapeHtml(error.prompt)}</strong><button class="filter-button retry-error" type="button" data-exercise="${escapeHtml(error.exercise_id)}">Дахин бодох</button><p>Өмнөх хариулт: ${escapeHtml(error.answer)} · Санамж: ${escapeHtml(error.hint)}</p></div>`
    ).join("") : `<p class="empty-state">Буруу хариултын түүх алга.</p>`}`;
    document.querySelectorAll(".retry-error").forEach((button) => button.addEventListener("click", () => {
      const target = document.querySelector(`.answer-form[data-id="${CSS.escape(button.dataset.exercise)}"]`);
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
      target?.querySelector("input").focus();
    }));
    $("#assessmentHistory").innerHTML = `<h3>Шалгалтын үр дүнгийн түүх</h3>${dashboard.assessments.length ? dashboard.assessments.map((item) =>
      `<div class="class-row"><span>${escapeHtml(item.title)}</span><strong>${item.points_earned}/${item.max_points}</strong></div>`
    ).join("") : `<p class="empty-state">Шалгалтын үр дүн хараахан байхгүй.</p>`}`;
  }

  function renderProfile() {
    const name = currentUser?.display_name || "Б. Цэрэндолгор";
    const role = currentUser?.role === "teacher" ? "Багшийн хэсэг" : currentUser ? "Сурагчийн хэсэг" : "Нэвтрээгүй";
    $("#profileName").textContent = name;
    $("#profileRole").textContent = role;
    const avatar = $("#profileAvatar");
    if (currentUser?.profile_image) {
      avatar.innerHTML = `<img src="${escapeHtml(currentUser.profile_image)}" alt="">`;
      avatar.classList.add("has-photo");
    } else {
      avatar.textContent = name.slice(0, 1).toLocaleUpperCase("mn");
      avatar.classList.remove("has-photo");
    }
    $("#welcomeName").textContent = name.toLocaleUpperCase("mn");
    welcomePhoto.src = currentUser?.profile_image || defaultWelcomePhoto;
    welcomePhoto.alt = currentUser?.display_name
      ? `${currentUser.display_name}-ийн профайл зураг`
      : "Таны профайл зураг";
    welcomePhoto.closest(".welcome-photo-frame").classList.remove("photo-missing");
    $("#loginButton").innerHTML = currentUser ? "<span>↪</span> Гарах" : "<span>↪</span> Нэвтрэх";
    $(".saved-state").innerHTML = currentUser ? "<i></i> Сервертэй холбогдсон" : "<i></i> Зочны горим";
    $("#classroom").classList.toggle("hidden", !currentUser);
    $("#changeLogoButton").classList.toggle("hidden", currentUser?.role !== "teacher");
    $("#changeCoverButton").classList.toggle("hidden", currentUser?.role !== "teacher");
  }

  $("#welcomePhoto").addEventListener("error", () => {
    $("#welcomePhoto").closest(".welcome-photo-frame").classList.add("photo-missing");
  });

  function applySiteCover(coverImage) {
    const cover = $(".welcome-section");
    if (!coverImage) {
      cover.style.removeProperty("--welcome-cover-image");
      return;
    }
    if (!/^\/media\/cover\/cover-[a-f0-9]{32}\.(?:png|jpg|webp)$/.test(coverImage)) {
      throw new Error("Нүүр зургийн холбоос буруу байна.");
    }
    cover.style.setProperty("--welcome-cover-image", `url("${coverImage}")`);
  }

  async function loadSiteCover() {
    const result = await api("/api/site/cover");
    applySiteCover(result.cover_image);
  }

  async function loadSiteLogo() {
    const result = await api("/api/site/logo");
    const logoImage = $("#brandLogoImage");
    if (result.logo_image) {
      logoImage.src = result.logo_image;
      logoImage.classList.remove("hidden");
      $("#brandMarkFallback").classList.add("hidden");
    } else {
      logoImage.removeAttribute("src");
      logoImage.classList.add("hidden");
      $("#brandMarkFallback").classList.remove("hidden");
    }
  }

  function openAuth(register = false) {
    setAuthMode(register);
    dialog.showModal();
  }

  function setAuthMode(register) {
    $("#authTitle").textContent = register ? "Бүртгүүлэх" : "Нэвтрэх";
    $("#authDescription").textContent = register ? "Хэрэглэгчийн төрлөө сонгож бүртгэл үүсгэнэ үү." : "Имэйл болон нууц үгээ оруулна уу.";
    $("#authSubmit").innerHTML = register ? "Бүртгэл үүсгэх <span>→</span>" : "Нэвтрэх <span>→</span>";
    $("#registerFields").classList.toggle("hidden", !register);
    $("#authPassword").autocomplete = register ? "new-password" : "current-password";
    $("#resetForm").classList.add("hidden");
    $("#authForm").classList.remove("hidden");
    $("#toggleRegister").textContent = register ? "Нэвтрэх хэсэг рүү буцах" : "Шинээр бүртгүүлэх";
  }

  async function refreshAssessments() {
    const result = await api("/api/assessments");
    $("#assessmentSelect").innerHTML = `<option value="">Шалгалт сонгоно уу</option>${result.assessments.map((item) =>
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)} · ${item.duration_minutes} минут</option>`).join("")}`;
  }

  async function refreshClassroom() {
    const host = $("#classroomContent");
    if (!currentUser) return;
    if (currentUser.role === "teacher") {
      const [report, assessmentResult, learningReport] = await Promise.all([
        api("/api/teacher/dashboard"),
        api("/api/assessments"),
        api("/api/teacher/learning")
      ]);
      const classes = report.classes || [];
      const classOptions = classes.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("");
      const exerciseOptions = exercises.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.topic)} · ${escapeHtml(item.level)} · ${escapeHtml(item.id)}</option>`).join("");
      const assessmentOptions = assessmentResult.assessments.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)} · ${item.duration_minutes} минут</option>`).join("");
      host.innerHTML = `<div class="teacher-tools"><a class="filter-button" href="/api/teacher/report.xlsx">Excel тайлан татах (.xlsx)</a><button class="filter-button" id="printTeacherReport" type="button">PDF болгох / хэвлэх</button></div>${newJoinCode ? `<div class="join-code-notice"><strong>Шинэ ангийн код: ${escapeHtml(newJoinCode)}</strong><span>Кодыг одоо сурагчидтай хуваалцаарай. Аюулгүй байдлын үүднээс дараа дахин харуулахгүй.</span></div>` : ""}<div class="classroom-grid">
        <form class="platform-card form-stack" id="createClassForm"><h3>Анги үүсгэх</h3><label for="newClassName">Ангийн нэр</label><select id="newClassName" required><option>12А</option><option>10А</option><option>10Б</option><option>11А</option><option>11Б</option></select><button class="primary-button action-button">Анги үүсгэх</button></form>
        <form class="platform-card form-stack" id="addStudentForm"><h3>Сурагч нэмэх</h3><p>Сурагчийн бүртгэлтэй имэйл эсвэл утасны дугаарыг оруулна.</p><label for="studentClass">Анги</label><select id="studentClass" required>${classOptions || "<option value=''>Эхлээд анги үүсгэнэ үү</option>"}</select><label for="studentEmail">Имэйл эсвэл утасны дугаар</label><input id="studentEmail" type="text" inputmode="email" required maxlength="254" autocomplete="off"><button class="primary-button action-button" ${!classes.length ? "disabled" : ""}>Сурагчийг ангид нэмэх</button></form>
        <form class="platform-card form-stack" id="assignForm"><h3>Дасгал даалгах</h3><label for="assignClass">Анги</label><select id="assignClass" required>${classOptions || "<option value=''>Эхлээд анги үүсгэнэ үү</option>"}</select><label for="assignExercise">Дасгал</label><select id="assignExercise" required>${exerciseOptions}</select><button class="primary-button action-button" ${!classes.length || !exercises.length ? "disabled" : ""}>Даалгах</button></form>
        <form class="platform-card form-stack" id="assignAssessmentForm"><h3>Шалгалт оноох</h3><label for="assessmentClass">Анги</label><select id="assessmentClass" required>${classOptions || "<option value=''>Эхлээд анги үүсгэнэ үү</option>"}</select><label for="assessmentToAssign">Шалгалт</label><select id="assessmentToAssign" required>${assessmentOptions}</select><button class="primary-button action-button" ${!classes.length || !assessmentOptions.length ? "disabled" : ""}>Шалгалт оноох</button></form>
      </div>
      <div class="platform-card"><h3>Ангийн гүйцэтгэл</h3>${classes.length ? `<div class="table-wrap"><table><thead><tr><th>Анги</th><th>Сурагч</th><th>Дасгал</th><th>Шалгалт</th><th>Дундаж гүйцэтгэл</th></tr></thead><tbody>${classes.map((item) => `<tr><td>${escapeHtml(item.name)}</td><td>${item.student_count}</td><td>${item.assignment_count}</td><td>${item.assessment_assignment_count}</td><td>${item.average_percent == null ? "—" : `${item.average_percent}%`}</td></tr>`).join("")}</tbody></table></div>` : `<p>Одоогоор анги үүсгээгүй байна.</p>`}</div>
      <div class="platform-card"><h3>Сурагч тус бүрийн тайлан</h3>${report.students.length ? `<div class="table-wrap"><table><thead><tr><th>Анги</th><th>Сурагч</th><th>Оролдлого</th><th>Оноо</th><th>Алдсан бодлого</th><th>Багшийн үнэлгээ</th></tr></thead><tbody>${report.students.map((item) => `<tr><td>${escapeHtml(item.class_name)}</td><td>${escapeHtml(item.display_name)}</td><td>${item.attempts}</td><td>${item.points}</td><td>${item.error_questions}</td><td>${item.teacher_reviews}</td></tr>`).join("")}</tbody></table></div>` : `<p>Ангид сурагч элсээгүй байна.</p>`}</div>
      <div class="platform-card"><h3>Сурагчдын хариулт</h3>${report.attempts.length ? `<div class="table-wrap"><table><thead><tr><th>Сурагч</th><th>Сэдэв</th><th>Түвшин</th><th>Хариулт</th><th>Оноо · багшийн үнэлгээ</th></tr></thead><tbody>${report.attempts.map((item) => `<tr><td>${escapeHtml(item.display_name)}</td><td>${escapeHtml(item.topic)}</td><td>${escapeHtml(item.level)}</td><td>${escapeHtml(item.answer)}</td><td><form class="review-form" data-attempt="${escapeHtml(item.id)}"><input type="number" min="0" max="${item.points}" value="${item.reviewed_points ?? item.points_earned}" aria-label="Багшийн оноо"><button class="filter-button">Хадгалах</button></form></td></tr>`).join("")}</tbody></table></div>` : `<p>Сурагчдын гүйцэтгэл хараахан бүртгэгдээгүй байна.</p>`}</div>
      <div class="platform-card"><h3>Цахим хичээлийн явц</h3>${learningReport.students.length ? `<div class="table-wrap"><table><thead><tr><th>Анги</th><th>Сурагч</th><th>Үзсэн хичээл</th><th>Дуусгасан хичээл</th><th>Мини/өдрийн сорил</th><th>Сорилын алдаа</th><th>Сүүлчийн сорил</th><th>Үйлдэл</th></tr></thead><tbody>${learningReport.students.map((item) => `<tr><td>${escapeHtml(item.class_name)}</td><td>${escapeHtml(item.display_name)}</td><td>${item.viewed_lessons}</td><td>${item.completed_lessons}</td><td>${item.quiz_attempts}</td><td>${item.quiz_errors}</td><td>${escapeHtml(item.last_quiz_at || "—")}</td><td><button class="filter-button remove-class-student" type="button" data-class="${escapeHtml(item.class_id)}" data-student="${escapeHtml(item.student_id)}">Ангиас хасах</button></td></tr>`).join("")}</tbody></table></div>` : `<p>Ангид бүртгэлтэй сурагч одоогоор байхгүй байна.</p>`}</div>
      <div class="platform-card"><h3>Сорилын алдааны шинжилгээ</h3>${learningReport.errors.length ? `<div class="table-wrap"><table><thead><tr><th>Сурагч</th><th>Хичээлийн ID</th><th>Асуулт</th><th>Хариулт</th><th>Зөв хариу</th><th>Огноо</th></tr></thead><tbody>${learningReport.errors.map((item) => `<tr><td>${escapeHtml(item.class_name)} · ${escapeHtml(item.display_name)}</td><td>${escapeHtml(item.lesson_id)}</td><td>${escapeHtml(item.prompt)}</td><td>${escapeHtml(item.answer || "(хоосон)")}</td><td>${escapeHtml(item.expected_answer)}</td><td>${escapeHtml(item.submitted_at)}</td></tr>`).join("")}</tbody></table></div>` : `<p>Сурагчдын сорилын алдаа бүртгэгдээгүй байна.</p>`}</div>
      <div class="platform-card"><h3>Шалгалтын гүйцэтгэл</h3>${report.assessments.length ? `<div class="table-wrap"><table><thead><tr><th>Сурагч</th><th>Шалгалт</th><th>Оноо</th><th>Боломжит оноо</th><th>Илгээсэн</th></tr></thead><tbody>${report.assessments.map((item) => `<tr><td>${escapeHtml(item.display_name)}</td><td>${escapeHtml(item.title)}</td><td>${item.points_earned ?? "—"}</td><td>${item.max_points}</td><td>${escapeHtml(item.submitted_at || "Үргэлжилж байна")}</td></tr>`).join("")}</tbody></table></div>` : `<p>Шалгалтын оролдлого бүртгэгдээгүй байна.</p>`}</div>`;
      $("#createClassForm").addEventListener("submit", createClass);
      $("#addStudentForm").addEventListener("submit", addStudentToClass);
      $("#assignForm").addEventListener("submit", createAssignment);
      $("#assignAssessmentForm").addEventListener("submit", assignAssessment);
      $("#printTeacherReport").addEventListener("click", () => printTeacherReport(report));
      document.querySelectorAll(".review-form").forEach((form) => form.addEventListener("submit", reviewStudentAttempt));
      const attemptRows = [...host.querySelectorAll(".review-form")].map((form) => form.closest("tr"));
      const rowsByStudent = new Map();
      report.attempts.forEach((attempt, index) => {
        const row = attemptRows[index];
        if (!row) return;
        const group = rowsByStudent.get(attempt.user_id) || { name: attempt.display_name, rows: [], firstRow: row };
        group.rows.push(row);
        rowsByStudent.set(attempt.user_id, group);
      });
      rowsByStudent.forEach((group) => {
        group.rows.forEach((row) => { row.classList.add("student-answer-detail"); row.hidden = true; });
        const summary = document.createElement("tr");
        summary.className = "student-answer-summary";
        summary.innerHTML = `<td colspan="5"><button class="student-answer-toggle" type="button" aria-expanded="false">${escapeHtml(group.name)}</button><span class="student-answer-hint">Хариултуудыг харах</span></td>`;
        group.firstRow.parentElement.insertBefore(summary, group.firstRow);
        summary.querySelector(".student-answer-toggle").addEventListener("click", () => {
          const expanded = summary.querySelector(".student-answer-toggle").getAttribute("aria-expanded") === "true";
          summary.querySelector(".student-answer-toggle").setAttribute("aria-expanded", String(!expanded));
          summary.querySelector(".student-answer-hint").textContent = expanded ? "Хариултуудыг харах" : "Хариултуудыг нуух";
          group.rows.forEach((row) => { row.hidden = expanded; });
        });
      });
      document.querySelectorAll(".remove-class-student").forEach((button) => {
        button.addEventListener("click", removeStudentFromClass);
      });
    } else {
      const [result, assignmentResult, assessmentAssignmentResult] = await Promise.all([
        api("/api/classes/my"),
        api("/api/assignments"),
        api("/api/assessment-assignments")
      ]);
      host.innerHTML = `<form class="platform-card form-stack" id="joinClassForm"><h3>Багшийн ангидаа элсэх</h3><label for="joinCode">Ангийн 8 тэмдэгт код</label><input id="joinCode" maxlength="8" required autocomplete="off"><button class="primary-button action-button">Ангид элсэх</button></form>
        <div class="platform-card"><h3>Миний даалгавар</h3>${assignmentResult.assignments.length ? assignmentResult.assignments.map((item) => `<div class="class-row"><span><strong>${escapeHtml(item.topic)}</strong> · ${escapeHtml(item.class_name)} · ${escapeHtml(item.level)}${item.due_at ? ` · Дуусах ${escapeHtml(item.due_at)}` : ""}</span><button class="filter-button assignment-go" type="button" data-assignment-exercise="${escapeHtml(item.exercise_id)}">${item.attempts ? "Дахин ажиллах" : "Ажиллах"}</button></div>`).join("") : "<p>Оноосон даалгавар байхгүй байна.</p>"}</div>
        <div class="platform-card"><h3>Оноосон шалгалтууд</h3>${assessmentAssignmentResult.assignments.length ? assessmentAssignmentResult.assignments.map((item) => `<div class="class-row"><span><strong>${escapeHtml(item.title)}</strong> · ${escapeHtml(item.class_name)} · ${item.duration_minutes} минут${item.due_at ? ` · Дуусах ${escapeHtml(item.due_at)}` : ""}</span><button class="filter-button assigned-test" type="button" data-assessment="${escapeHtml(item.assessment_id)}">${item.submitted_attempts ? "Дахин өгөх" : "Шалгалт эхлүүлэх"}</button></div>`).join("") : "<p>Оноосон шалгалт байхгүй байна.</p>"}</div>
        <div class="platform-card"><h3>Миний ангиуд</h3>${result.classes.length ? result.classes.map((item) => `<div class="class-row"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.teacher_name || "")}</span></div>`).join("") : "<p>Код ашиглан ангид элсээрэй.</p>"}</div>
        <div class="platform-card"><h3>Математикийн сорил</h3><p>Зөв хариултаар оноо цуглуулж, онооны жагсаалтад ороорой.</p><div class="game-controls"><select id="gameExercise" aria-label="Сорилын бодлого">${exercises.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.topic)} · ${escapeHtml(item.level)}</option>`).join("")}</select><button class="filter-button" id="startGame" type="button">Сорил эхлүүлэх</button></div><div id="gameArea"></div><div id="leaderboard"></div></div>`;
      $("#joinClassForm").addEventListener("submit", joinClass);
      document.querySelectorAll(".assigned-test").forEach((button) => button.addEventListener("click", () => {
        $("#assessmentSelect").value = button.dataset.assessment;
        startAssessment();
        $("#tests").scrollIntoView({ behavior: "smooth" });
      }));
      document.querySelectorAll(".assignment-go").forEach((button) => button.addEventListener("click", () => {
        const target = document.querySelector(`.answer-form[data-id="${CSS.escape(button.dataset.assignmentExercise)}"]`);
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
        target?.querySelector("input").focus();
      }));
      $("#startGame").addEventListener("click", startGame);
      refreshLeaderboard();
    }
  }

  async function refreshData() {
    if (!currentUser) {
      exercises = [];
      $("#exerciseList").innerHTML = `<p class="empty-state">Дасгал ажиллаж, оноогоо хадгалахын тулд <button class="inline-button" id="openLoginFromExercises" type="button">нэвтэрнэ үү</button>.</p>`;
      $("#openLoginFromExercises").addEventListener("click", () => openAuth());
      $("#assessmentWork").innerHTML = `<p class="empty-state">Шалгалт өгөхийн тулд нэвтэрнэ үү.</p>`;
      $("#classroomContent").innerHTML = `<p class="empty-state">Анги болон сорил нэвтэрсний дараа боломжтой.</p>`;
      renderProgress();
      renderIndependentPractice();
      return;
    }
    const level = $("#exerciseLevel").value;
    const chapter = $("#exerciseChapter").value;
    const [exerciseResult, dashboard] = await Promise.all([
      api(`/api/exercises?chapter_id=${encodeURIComponent(chapter)}${level ? `&level=${encodeURIComponent(level)}` : ""}`),
      currentUser.role === "student" ? api("/api/student/dashboard") : Promise.resolve(null)
    ]);
    exercises = exerciseResult.exercises;
    renderExercises();
    renderIndependentPractice();
    if (dashboard) renderProgress(dashboard);
    else renderProgress();
    if ($("#assessmentForm")) $("#assessmentWork").innerHTML = `<p class="empty-state">Шалгалтаа сонгоод “Шалгалт эхлүүлэх” товчийг дарна уу.</p>`;
    await Promise.all([refreshAssessments(), refreshClassroom()]);
  }

  async function loadLearning() {
    const [catalog, daily] = await Promise.all([
      api("/api/learning/lessons"),
      api("/api/learning/quizzes/daily")
    ]);
    learningCatalog = catalog;
    dailyQuiz = daily;
    renderLearning();
    renderDailyQuiz();
    if (currentUser?.role === "student") await refreshLearningState();
  }

  async function loadEeshQuizzes() {
    try {
      const [quizResult, materialResult] = await Promise.all([
        api("/api/eesh/quizzes"),
        api("/api/eesh/materials")
      ]);
      eeshMaterials = materialResult.materials;
      renderEeshQuizCatalog(quizResult.quizzes, eeshMaterials);
    } catch (error) {
      console.error("ЭЕШ сорилыг ачаалж чадсангүй.", error);
      $("#eeshQuizContent").innerHTML = `<p class="empty-state">ЭЕШ сорилыг ачаалж чадсангүй. Серверээ шалгаад дахин ачаална уу.</p>`;
    }
  }

  function renderEeshQuizCatalog(quizzes, materials) {
    const host = $("#eeshQuizContent");
    const quizSection = quizzes.length
      ? `<div class="platform-card"><div class="test-controls"><label for="eeshQuizSelect">Он · хувилбар</label><select id="eeshQuizSelect" aria-label="ЭЕШ сорил сонгох"><option value="">Сорил сонгоно уу</option>${quizzes.map((quiz) =>
        `<option value="${escapeHtml(quiz.id)}">${escapeHtml(quiz.year)} · ${escapeHtml(quiz.variant)} хувилбар · ${quiz.duration_minutes} минут</option>`).join("")}</select><button class="filter-button" id="startEeshQuiz" type="button">Сорил эхлүүлэх</button></div><div class="assessment-work" id="eeshQuizWork"><p class="empty-state">Оны сорилоо сонгоод эхлүүлнэ үү.</p></div></div>`
      : `<div class="platform-card"><h3>Интерактив сорил</h3><p class="empty-state">Одоогоор интерактив асуулт нэмээгүй. Шалгасан бодлого, зөв хариу, тайлбарыг <code>data/eesh_quizzes.json</code>-д оруулсны дараа хугацаатай сорил энд гарна.</p></div>`;
    const materialSection = materials.length
      ? `<div class="platform-card"><h3>ЭЕШ-ийн эх материал · ${materials.length}</h3><p>Слайдыг апп дотроо үзэж, хариулт эсвэл тэмдэглэлээ хадгална. Эх PPTX-үүдэд бодолт, зөв хариу харагддаг тул энэ нь бэлтгэлийн горим бөгөөд автоматаар оноо тавихгүй.</p><div class="eesh-material-list">${materials.map((material) =>
        `<article class="eesh-material-card"><strong>${escapeHtml(material.title)}</strong><button class="filter-button" type="button" data-start-eesh-material="${escapeHtml(material.id)}">Апп дотор нээж бөглөх</button><a class="filter-button" href="${escapeHtml(material.download_url)}" download>PowerPoint татах</a></article>`).join("")}</div></div>`
      : `<p class="empty-state">ЭЕШ-ийн эх материал хараахан бүртгээгүй байна.</p>`;
    host.innerHTML = `${quizSection}${materialSection}<div id="eeshMaterialWork"></div>`;
  }

  async function startEeshMaterialPractice(materialId) {
    if (!requireStudent()) return;
    if (activeEeshMaterial && !await persistEeshMaterialResponse()) return;
    const material = eeshMaterials.find((item) => item.id === materialId);
    if (!material) return showToast("ЭЕШ материал олдсонгүй.");
    try {
      const result = await api(`/api/eesh/materials/${encodeURIComponent(materialId)}/responses`);
      activeEeshMaterial = material;
      eeshMaterialResponses = result.responses;
      activeEeshSlide = 1;
      renderEeshMaterialViewer();
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderEeshMaterialViewer() {
    const host = $("#eeshMaterialWork");
    if (!host || !activeEeshMaterial) return;
    const material = activeEeshMaterial;
    const slideNumber = activeEeshSlide;
    host.innerHTML = `<section class="eesh-viewer platform-card" aria-label="${escapeHtml(material.title)}">
      <div class="test-heading"><h3>${escapeHtml(material.title)}</h3><strong>Слайд ${slideNumber} / ${material.slide_count}</strong></div>
      <div class="eesh-slide-navigation"><button class="filter-button" type="button" data-eesh-step="-1" ${slideNumber <= 1 ? "disabled" : ""}>Өмнөх</button><button class="filter-button" type="button" data-eesh-step="1" ${slideNumber >= material.slide_count ? "disabled" : ""}>Дараах</button><button class="filter-button" type="button" data-close-eesh-viewer>Жагсаалт руу буцах</button></div>
      <img class="eesh-slide-image" src="/api/eesh/materials/${encodeURIComponent(material.id)}/slides/${slideNumber}" alt="${escapeHtml(material.title)} · слайд ${slideNumber}">
      <form class="eesh-response-form" id="eeshResponseForm" data-slide-number="${slideNumber}">
        <label for="eeshSlideResponse">Энэ слайдын хариу эсвэл тэмдэглэл</label>
        <textarea id="eeshSlideResponse" name="answer" maxlength="5000" rows="4">${escapeHtml(eeshMaterialResponses[String(slideNumber)] || "")}</textarea>
        <div class="eesh-response-actions"><button class="primary-button action-button" type="submit">Хариулт хадгалах</button><span class="eesh-save-status" aria-live="polite"></span></div>
      </form>
    </section>`;
    $("#eeshResponseForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      await persistEeshMaterialResponse(true);
    });
  }

  async function persistEeshMaterialResponse(showSuccess = false) {
    const form = $("#eeshResponseForm");
    if (!form || !activeEeshMaterial) return true;
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector(".eesh-save-status");
    const slideNumber = Number(form.dataset.slideNumber);
    const answer = form.elements.answer.value;
    button.disabled = true;
    status.textContent = "Хадгалж байна…";
    try {
      await post(`/api/eesh/materials/${encodeURIComponent(activeEeshMaterial.id)}/responses`, {
        slide_number: slideNumber,
        answer
      });
      eeshMaterialResponses[String(slideNumber)] = answer;
      status.textContent = "Хадгалагдлаа";
      if (showSuccess) showToast("Хариулт хадгалагдлаа.");
      return true;
    } catch (error) {
      status.textContent = "Хадгалж чадсангүй";
      showToast(error.message);
      return false;
    } finally {
      button.disabled = false;
    }
  }

  async function startEeshQuiz() {
    if (!requireStudent()) return;
    const quizId = $("#eeshQuizSelect")?.value;
    if (!quizId) return showToast("ЭЕШ сорилоо сонгоно уу.");
    try {
      const attempt = await post(`/api/eesh/quizzes/${encodeURIComponent(quizId)}/start`, {});
      const result = await api(`/api/eesh/quizzes/${encodeURIComponent(quizId)}`);
      renderEeshQuiz(result.quiz, attempt);
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderEeshQuiz(quiz, attempt) {
    window.clearInterval(eeshQuizTimer);
    const host = $("#eeshQuizWork");
    const end = new Date(attempt.expires_at).getTime();
    const renderTimer = () => {
      const seconds = Math.max(0, Math.floor((end - Date.now()) / 1000));
      $("#eeshQuizTimer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      if (!seconds) {
        window.clearInterval(eeshQuizTimer);
        $("#eeshQuizSubmit").disabled = true;
        host.querySelectorAll("input").forEach((input) => { input.disabled = true; });
        showToast("ЭЕШ сорилын хугацаа дууслаа. Хариултыг илгээх боломжгүй.");
      }
    };
    host.innerHTML = `<div class="test-heading"><h3>${escapeHtml(quiz.year)} оны ЭЕШ · ${escapeHtml(quiz.variant)} хувилбар</h3><strong id="eeshQuizTimer"></strong></div>
      <form id="eeshQuizForm" data-attempt-id="${escapeHtml(attempt.attempt_id)}">${quiz.questions.map((question, index) => `<label class="test-question"><strong>${index + 1}. ${escapeHtml(question.prompt)} · ${question.points} оноо</strong><input name="${escapeHtml(question.id)}" maxlength="1000" autocomplete="off" required></label>`).join("")}
      <button class="primary-button action-button" id="eeshQuizSubmit">Сорил дуусгах, илгээх</button></form><div id="eeshQuizResult" aria-live="polite"></div>`;
    renderTimer();
    if (end > Date.now()) eeshQuizTimer = window.setInterval(renderTimer, 1000);
    $("#eeshQuizForm").addEventListener("submit", submitEeshQuiz);
  }

  async function submitEeshQuiz(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const answers = Object.fromEntries(new FormData(form).entries());
    const button = $("#eeshQuizSubmit");
    button.disabled = true;
    window.clearInterval(eeshQuizTimer);
    try {
      const result = await post(`/api/eesh/attempts/${encodeURIComponent(form.dataset.attemptId)}/submit`, { answers });
      const selectedQuiz = $("#eeshQuizSelect").value;
      const quiz = (await api(`/api/eesh/quizzes/${encodeURIComponent(selectedQuiz)}`)).quiz;
      const questions = new Map(quiz.questions.map((question) => [question.id, question]));
      $("#eeshQuizResult").innerHTML = `<div class="platform-card"><h3>Үр дүн: ${result.points_earned}/${result.points_possible} оноо · ${result.percentage}%</h3>${Object.entries(result.answers).map(([id, answer]) => {
        const question = questions.get(id);
        return `<div class="answer-review"><strong class="${answer.correct ? "correct" : "incorrect"}">${answer.correct ? "Зөв" : "Буруу"} · ${answer.points} оноо</strong><span>${escapeHtml(question?.prompt || id)}</span><p>Зөв хариу: ${escapeHtml(answer.expected_answer)}</p><p>${escapeHtml(answer.explanation)}</p></div>`;
      }).join("")}</div>`;
      form.remove();
      showToast("ЭЕШ сорилын үр дүн хадгалагдлаа.");
    } catch (error) {
      button.disabled = false;
      showToast(error.message);
    }
  }

  async function refreshLearningState() {
    if (currentUser?.role !== "student") {
      learningState = { lessons: [], attempts: [], errors: [] };
      renderLearning();
      renderDailyQuiz();
      return;
    }
    learningState = await api("/api/student/learning");
    renderLearning();
    renderDailyQuiz();
  }

  function lessonProgress(lessonId) {
    return learningState.lessons.find((item) => item.lesson_id === lessonId);
  }

  function renderLearning() {
    const host = $("#learningContent");
    const formulaReference = $("#formulaReference");
    const glossaryReference = $("#glossaryReference");
    const formulaEntries = learningCatalog.lessons.flatMap((lesson) => lesson.formulas.map((formula) => ({ ...formula, lesson })));
    const termEntries = learningCatalog.lessons.flatMap((lesson) => lesson.glossary.map((entry) => ({ ...entry, lesson })));
    renderReferenceBanks(formulaReference, glossaryReference, formulaEntries, termEntries);
    if (!learningCatalog.lessons.length) {
      host.innerHTML = `<p class="empty-state">Эх сурвалжтай тулган бэлтгэсэн цахим хичээл одоогоор алга.</p>`;
      return;
    }
    const available = learningCatalog.chapters.filter((chapter) => chapter.lesson_count);
    if (selectedLearningChapter && !available.some((chapter) => chapter.id === selectedLearningChapter)) {
      selectedLearningChapter = null;
    }
    const lessonChoices = selectedLearningChapter
      ? learningCatalog.lessons.filter((lesson) => lesson.chapter_id === selectedLearningChapter)
      : [];
    if (!lessonChoices.some((lesson) => lesson.id === activeLessonId)) {
      activeLessonId = null;
    }
    const active = learningCatalog.lessons.find((lesson) => lesson.id === activeLessonId);
    const chapterCards = learningCatalog.chapters.map((chapter) => chapter.lesson_count
      ? `<button class="learning-chapter${chapter.id === selectedLearningChapter ? " selected" : ""}" type="button" data-learning-chapter="${escapeHtml(chapter.id)}"><strong>${escapeHtml(chapter.number)} · ${escapeHtml(chapter.title)}</strong><span>${chapter.lesson_count} цахим хичээл</span></button>`
      : `<div class="learning-chapter unavailable"><strong>${escapeHtml(chapter.number)} · ${escapeHtml(chapter.title)}</strong><span>Эх материалтай тулгаж бэлтгэж байна</span></div>`
    ).join("");
    const lessonTabs = lessonChoices.map((lesson) => {
      const progress = lessonProgress(lesson.id);
      return `<button type="button" class="lesson-tab${lesson.id === activeLessonId ? " selected" : ""}" data-learning-lesson="${escapeHtml(lesson.id)}">
        <span>${progress?.completed_at ? "✓" : "○"}</span><strong>${escapeHtml(lesson.title)}</strong><small>${escapeHtml(lesson.topic)}</small></button>`;
    }).join("");
    const lessonCoverage = available.map((chapter) => `${chapter.number} бүлэг · ${chapter.lesson_count} сэдэв`).join(" · ");
    const unpreparedChapters = chapters.length - available.length;
    const lessonSelectionPrompt = selectedLearningChapter
      ? "Үргэлжлүүлэхийн тулд бүлгийн сэдвээс сонгоно уу."
      : "Эхлээд бүлгээ, дараа нь судлах сэдвээ сонгоно уу.";
    host.innerHTML = `<div class="learning-source-note"><strong>Хөтөлбөрийн хамрах хүрээ</strong><span>Цахим хичээлтэй бүлэг: ${escapeHtml(lessonCoverage)}.${unpreparedChapters ? ` Бусад ${unpreparedChapters} бүлгийн материал хараахан бэлтгэгдээгүй.` : ""}</span></div>
      <section class="learning-feature-card medle-resource"><div class="feature-kicker">АЛБАН ЁСНЫ ГАДААД ЭХ СУРВАЛЖ</div><h3>MEDLE · XII ангийн цахим хичээл</h3><p>MEDLE платформ дахь нэгж хичээлийн хуудсыг нээж, нийтэлсэн материалтай танилцаарай.</p><a class="filter-button" href="https://medle.mn/primarySchool/2/12/5" target="_blank" rel="noopener noreferrer">MEDLE дээр хичээл нээх ↗</a></section>
      <div class="learning-chapters">${chapterCards}</div>
      <div class="learning-layout"><nav class="lesson-list-nav" aria-label="Цахим хичээл">${lessonTabs || `<p class="empty-state">${escapeHtml(lessonSelectionPrompt)}</p>`}</nav>
        <div class="lesson-detail">${active ? renderLesson(active) : `<p class="empty-state">${escapeHtml(lessonSelectionPrompt)}</p>`}</div></div>
      <div id="dailyChallenge"></div>
      <div class="learning-tool-grid">
        <section class="learning-feature-card"><div class="feature-kicker">ХӨДӨЛГӨӨНТ ДҮРСЛЭЛ</div><h3>Видео хичээл</h3><p>Эх материалд видео файл эсвэл албан видео холбоос ирээгүй тул зохиомол тоглуулагч, баталгаагүй видео нийтлээгүй. Одоохондоо алхамтай жишээ болон график лабораторийг ашиглаарай.</p></section>
        <section class="learning-feature-card"><div class="feature-kicker">ТУСЛАХ БАГШ</div><h3>AI математикийн туслах</h3><p>AI загварын үйлчилгээ, түлхүүр тохируулагдаагүй. Сурагчийн асуултыг гуравдагч AI үйлчилгээнд илгээх зөвшөөрөлгүйгээр AI гэж дүр эсгэсэн хариу өгөхгүй. Баталгаатай тайлбар, томьёо, жишээ энэ хичээлд нээлттэй.</p></section>
      </div>
      <section class="learning-feature-card independent-panel"><div class="feature-kicker">БИЕ ДААЛТ</div><h3>Дасгалын сангаас бие даан ажиллах</h3><p>Хариулт одоогийн бодлогын сангаар автоматаар шалгагдаж, оролдлого ба оноо таны бүртгэлд хадгалагдана.</p><div id="independentPractice"></div></section>`;
    renderGraphLab();
    renderDailyQuiz();
  }

  function renderReferenceBanks(formulaReference, glossaryReference, formulaEntries, termEntries) {
    const formulas = formulaEntries.map((item) => `<article class="formula-card searchable-formula" data-search="${escapeHtml(`${item.name} ${item.formula} ${item.meaning} ${item.lesson.topic}`.toLocaleLowerCase("mn"))}"><span>${escapeHtml(item.lesson.topic)} · ${escapeHtml(item.name)}</span><strong>${escapeHtml(item.formula)}</strong><p>${escapeHtml(item.meaning)}</p></article>`).join("");
    const terms = termEntries.map((item) => `<article class="glossary-card searchable-term" data-search="${escapeHtml(`${item.term} ${item.definition} ${item.lesson.topic}`.toLocaleLowerCase("mn"))}"><strong>${escapeHtml(item.term)}</strong><span>${escapeHtml(item.definition)}</span></article>`).join("");
    formulaReference.innerHTML = `<h3 id="formulaReferenceTitle">Бүх бүлгийн томьёоны цахим сан</h3><p>Доорх томьёонууд нь одоогоор эхтэй тулган нийтэлсэн I бүлгийн томьёо. Баталгаажаагүй бүлэгт томьёо таамгаар нэмээгүй.</p><label class="reference-search">Томьёо хайх<input id="formulaSearch" type="search" placeholder="Тэмдэглэгээ, нэрээр хайх"></label><div class="formula-grid" id="formulaBank">${formulas || `<p class="empty-state">Одоогоор баталгаажсан томьёо алга.</p>`}</div>`;
    glossaryReference.innerHTML = `<h3 id="glossaryReferenceTitle">Математикийн нэр томьёоны тайлбар толь</h3><label class="reference-search">Нэр томьёо хайх<input id="glossarySearch" type="search" placeholder="Тайлбар хайх"></label><div class="glossary-grid" id="glossaryBank">${terms || `<p class="empty-state">Одоогоор тайлбар тольд нэр томьёо алга.</p>`}</div>`;
  }

  function renderLesson(lesson) {
    const progress = lessonProgress(lesson.id);
    const formulas = lesson.formulas.map((item) => `<article class="formula-card"><span>${escapeHtml(item.name)}</span><strong>${escapeHtml(item.formula)}</strong><p>${escapeHtml(item.meaning)}</p></article>`).join("");
    const examples = lesson.worked_examples.map((item, index) => `<details class="worked-example"><summary>Жишээ ${index + 1}: ${escapeHtml(item.prompt)}</summary><ol>${item.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol><p class="answer-key">Хариу: ${escapeHtml(item.answer)}</p></details>`).join("");
    const questions = lesson.quiz.map((question, index) => `<label class="test-question"><strong>${index + 1}. ${escapeHtml(question.prompt)} · ${question.points} оноо</strong><input name="${escapeHtml(question.id)}" maxlength="1000" autocomplete="off" required></label>`).join("");
    const errors = learningState.errors.filter((error) => error.lesson_id === lesson.id);
    const quizResult = learningQuizResults.get(lesson.id);
    const glossary = lesson.glossary.map((term) => `<article class="glossary-card"><strong>${escapeHtml(term.term)}</strong><span>${escapeHtml(term.definition)}</span></article>`).join("");
    const connections = lesson.connections.map((item) => {
      const target = learningCatalog.lessons.find((other) => other.id === item.lesson_id);
      return target ? `<button class="connection-link" type="button" data-learning-lesson="${escapeHtml(target.id)}" data-learning-chapter="${escapeHtml(target.chapter_id)}">${escapeHtml(item.label)} →</button>` : "";
    }).join("");
    const result = lesson.application;
    const lessonMedia = lesson.media || [];
    const mediaCards = lessonMedia.map((media) => {
      const size = `${Math.max(1, Math.ceil(media.size_bytes / (1024 * 1024)))} MB`;
      const content = media.media_type === "video"
        ? `<video controls preload="metadata" src="${escapeHtml(media.url)}">Таны хөтөч видеог тоглуулж чадсангүй.</video><a class="lesson-media-link" href="${escapeHtml(media.url)}" download>Видеог татах ↓</a>`
        : `<a class="lesson-media-link" href="${escapeHtml(media.url)}" ${media.media_type === "presentation" ? "download" : 'target="_blank" rel="noopener noreferrer"'}>${media.media_type === "presentation" ? "Татаж авах ↓" : "PDF нээх ↗"}</a>`;
      return `<article class="lesson-media-item"><div class="lesson-media-meta"><strong>${escapeHtml(media.title)}</strong><span>${escapeHtml(media.original_name)} · ${size}</span></div>${content}${media.can_delete ? `<button class="filter-button lesson-media-delete" type="button" data-delete-lesson-media="${escapeHtml(media.id)}">Устгах</button>` : ""}</article>`;
    }).join("");
    const uploadForm = currentUser?.role === "teacher"
      ? `<form class="lesson-media-upload" data-upload-lesson-media="${escapeHtml(lesson.id)}"><label>Материалын нэр<input name="title" maxlength="120" placeholder="Жишээ: Нэгж хичээлийн танилцуулга" required></label><label>Файл · PPT, PPTX, PDF, MP4, MOV, WebM эсвэл MKV<input name="file" type="file" accept=".ppt,.pptx,.pdf,.mp4,.m4v,.mov,.webm,.mkv,application/pdf,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.presentationml.presentation,video/*" required></label><small>Нэг файл 100 MB хүртэл. Материал зөвхөн энэ бүлэг, сэдэвт холбогдоно.</small><button class="primary-button action-button" type="submit">Материал нэмэх</button></form>`
      : "";
    const mediaSection = `<section class="lesson-media-section"><div class="lesson-media-heading"><h4>Нэмэлт хичээлийн материал</h4><span>${lessonMedia.length}</span></div>${mediaCards || `<p class="empty-state">Энэ сэдэвт нэмэлт PPT, PDF эсвэл видео одоогоор алга.</p>`}${uploadForm}</section>`;
    return `<article class="lesson-article">
      <div class="lesson-title-row"><div><div class="feature-kicker">${escapeHtml(lesson.topic)} · ${escapeHtml(lesson.objective_codes.join(", "))}</div><h3>${escapeHtml(lesson.title)}</h3></div><span class="lesson-progress-pill">${progress?.completed_at ? "Судалсан ✓" : progress?.viewed_at ? "Үзсэн" : "Шинэ хичээл"}</span></div>
      <p class="lesson-intro">${escapeHtml(lesson.intro)}</p><p class="lesson-source">${escapeHtml(lesson.source_note)} · Үнэлгээний шалгуур ${escapeHtml(lesson.criteria.join(", "))}</p>
      ${mediaSection}
      <section class="lesson-block"><h4>1. Онолын тайлбар</h4><ul class="lesson-theory">${lesson.theory.map((paragraph) => `<li>${escapeHtml(paragraph)}</li>`).join("")}</ul></section>
      <section class="lesson-block"><h4>2. Алхам алхмаар бодолт</h4>${examples}</section>
      <section class="lesson-block"><h4>3. Энэ хичээлийн томьёо</h4><div class="formula-grid">${formulas}</div></section>
      ${lesson.id === "absolute-inequalities" ? `<section class="lesson-block"><h4>4. Интерактив графикийн лаборатори</h4><p>Төв a болон зай b-г өөрчилж y=|x−a| график, |x−a|&lt;b шийдийн завсрыг ажигла.</p><div id="graphLab"></div></section>` : ""}
      <section class="lesson-block application-block"><h4>Амьдралтай холбосон бодлого</h4><strong>${escapeHtml(result.prompt)}</strong><details class="worked-example"><summary>Бодолтыг нээх</summary><ol>${result.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol><p class="answer-key">Хариу: ${escapeHtml(result.answer)}</p></details></section>
      <section class="lesson-block"><h4>Хичээлийн дараах мини сорил</h4><p>Нийт онооны 70%-д хүрвэл хичээлийг судалсанд тооцно. Дүн, хариултын алдаа серверт хадгалагдана.</p><form class="learning-quiz-form" data-quiz="${escapeHtml(lesson.id)}">${questions}<button class="primary-button action-button">Сорил илгээх</button></form><div class="quiz-feedback" aria-live="polite">${quizResult ? `<strong class="${quizResult.passed ? "correct" : "incorrect"}">${quizResult.passed ? "Хичээл амжилттай судлагдлаа." : "Дахин судлаад сорилоо давтан өгөөрэй."} · ${quizResult.points_earned}/${quizResult.points_possible}</strong>${quizResult.answers.map((item) => `<div class="answer-review"><strong class="${item.correct ? "correct" : "incorrect"}">${item.correct ? "Зөв" : "Зөв хариу: " + escapeHtml(item.expected_answer)}</strong><p>${escapeHtml(item.explanation)}</p></div>`).join("")}` : ""}</div></section>
      <section class="lesson-block"><h4>Өмнөх алдаагаа засах · ${errors.length}</h4>${errors.length ? errors.slice(0, 5).map((error) => `<div class="learning-error"><strong>${escapeHtml(error.prompt)}</strong><span>Таны хариу: ${escapeHtml(error.answer || "(хоосон)")} · Зөв хариу: ${escapeHtml(error.expected_answer)}</span><p>${escapeHtml(error.explanation)}</p>${error.quiz_type === "mini" ? `<button class="filter-button retry-learning-quiz" type="button" data-retry-lesson="${escapeHtml(lesson.id)}" data-retry-question="${escapeHtml(error.question_id)}">Энэ асуултыг дахин бодох</button>` : `<button class="filter-button retry-learning-exercise" type="button" data-exercise="${escapeHtml(error.question_id)}">Дасгалын санд дахин ажиллах</button>`}</div>`).join("") : `<p class="empty-state">Энэ хичээлийн сорилд одоогоор алдаа бүртгэгдээгүй.</p>`}</section>
      <section class="lesson-block"><h4>Сэдэв хоорондын холбоос</h4><div class="connection-list">${connections}</div></section>
      <section class="lesson-block"><h4>Түлхүүр нэр томьёо</h4><div class="glossary-grid">${glossary}</div></section>
      <section class="lesson-block"><h4>Сэдвийн дүгнэлт</h4><ul class="lesson-recap">${lesson.recap.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>
      <section class="lesson-block"><h4>Шалгалтад бэлтгэх</h4><p>I бүлгийн шалгалтын асуултууд нийтлэгдсэн дасгалын сангаас сонгогдоно.</p><button class="filter-button exam-prep-start" type="button">I бүлгийн сорилд бэлдэх</button></section>
      </article>
      `;
  }

  async function uploadLessonMedia(event) {
    event.preventDefault();
    if (currentUser?.role !== "teacher") {
      showToast("Нэмэлт материал зөвхөн багшийн эрхээр байршуулна.");
      return;
    }
    const form = event.target;
    const file = form.elements.file.files[0];
    if (!file) {
      showToast("Байршуулах файлаа сонгоно уу.");
      return;
    }
    if (file.size > 100 * 1024 * 1024) {
      showToast("Файлын хэмжээ 100 MB-аас хэтрэхгүй байна.");
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const query = new URLSearchParams({
        title: form.elements.title.value.trim(),
        filename: file.name,
      });
      await api(
        `/api/learning/lessons/${encodeURIComponent(form.dataset.uploadLessonMedia)}/media?${query}`,
        { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } }
      );
      await loadLearning();
      showToast("Нэмэлт материал хичээлд хадгалагдлаа.");
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  function renderGraphLab() {
    const lab = $("#graphLab");
    if (!lab) return;
    lab.innerHTML = `<div class="graph-controls"><label>Төв a <input id="graphCenter" type="range" min="-4" max="4" step="1" value="1"><output id="graphCenterValue">1</output></label><label>Зай b <input id="graphRadius" type="range" min="1" max="4" step="1" value="3"><output id="graphRadiusValue">3</output></label></div><div class="graph-canvas"><svg id="absoluteGraph" viewBox="0 0 640 300" role="img" aria-label="y тэнцүү абсолют x хасах a график"></svg></div><p class="graph-interpretation" id="graphInterpretation"></p>`;
    updateGraph();
  }

  function updateGraph() {
    const svg = $("#absoluteGraph");
    if (!svg) return;
    const center = Number($("#graphCenter").value);
    const radius = Number($("#graphRadius").value);
    const sx = (x) => 320 + x * 40;
    const sy = (y) => 260 - y * 28;
    const grid = [];
    for (let x = -7; x <= 7; x += 1) grid.push(`<line class="graph-grid-line" x1="${sx(x)}" y1="20" x2="${sx(x)}" y2="260"></line>`);
    for (let y = 0; y <= 8; y += 1) grid.push(`<line class="graph-grid-line" x1="40" y1="${sy(y)}" x2="600" y2="${sy(y)}"></line>`);
    const labels = [];
    for (let x = -6; x <= 6; x += 2) labels.push(`<text class="graph-label" x="${sx(x)}" y="282" text-anchor="middle">${x}</text>`);
    const points = [];
    for (let x = -7; x <= 7; x += 0.1) points.push(`${sx(x).toFixed(1)},${sy(Math.abs(x - center)).toFixed(1)}`);
    const lower = center - radius;
    const upper = center + radius;
    svg.innerHTML = `${grid.join("")}<line class="graph-axis" x1="40" y1="${sy(0)}" x2="600" y2="${sy(0)}"></line><line class="graph-axis" x1="${sx(0)}" y1="20" x2="${sx(0)}" y2="260"></line><polyline class="graph-curve" points="${points.join(" ")}"></polyline><line class="graph-limit" x1="40" y1="${sy(radius)}" x2="600" y2="${sy(radius)}"></line><circle class="graph-point" cx="${sx(center)}" cy="${sy(0)}" r="5"></circle>${labels.join("")}<text class="graph-label" x="606" y="${sy(radius) - 5}">b=${radius}</text>`;
    $("#graphCenterValue").textContent = String(center);
    $("#graphRadiusValue").textContent = String(radius);
    $("#graphInterpretation").textContent = `|x−${center}|<${radius} шийд: ${lower}<x<${upper}. График y=|x−${center}| нь y=${radius} шулуунаас доош байх x-ийн утгуудыг сонгоно.`;
  }

  function renderDailyQuiz() {
    const host = $("#dailyChallenge");
    if (!host || !dailyQuiz) return;
    const question = dailyQuiz.questions[0];
    const prior = learningState.attempts.find((attempt) => attempt.quiz_id === dailyQuiz.quiz_id);
    const results = dailyResult?.quiz_id === dailyQuiz.quiz_id
    ? `<div class="quiz-feedback"><strong class="${dailyResult.answers[0].correct ? "correct" : "incorrect"}">${dailyResult.answers[0].correct ? "Зөв хариуллаа!" : `Зөв хариу: ${escapeHtml(dailyResult.answers[0].expected_answer)}`} · ${dailyResult.points_earned}/${dailyResult.points_possible}</strong><p>${escapeHtml(dailyResult.answers[0].explanation)}</p></div>`
    : "";
    host.innerHTML = `<div class="learning-feature-card daily-card"><div class="feature-kicker">ӨДӨР БҮР · ${escapeHtml(dailyQuiz.quiz_id.slice(-2))}</div><h3>${escapeHtml(dailyQuiz.title)}</h3><p>Бодлогыг I бүлгийн нийтлэгдсэн дасгалын сангаас сонгов. Өдөрт нэг удаа илгээж болно.</p>${prior ? `<div class="quiz-feedback"><strong>Өдрийн сорил гүйцэтгэсэн</strong><p>Оноо: ${prior.points_earned}/${prior.max_points} · ${escapeHtml(prior.submitted_at)}</p></div>` : `<form class="daily-quiz-form" data-question="${escapeHtml(question.id)}"><strong>${escapeHtml(question.prompt)}</strong><label>Таны хариу<input name="answer" required maxlength="1000" autocomplete="off"></label><button class="primary-button action-button">Өдрийн сорил илгээх</button></form>`}${results}</div>`;
  }

  async function openLesson(lessonId, markViewed = true) {
    const lesson = learningCatalog.lessons.find((item) => item.id === lessonId);
    if (!lesson) return;
    activeLessonId = lesson.id;
    selectedLearningChapter = lesson.chapter_id;
    renderLearning();
    if (markViewed && currentUser?.role === "student") {
      try {
        await post(`/api/learning/lessons/${encodeURIComponent(lesson.id)}/view`, {});
        learningState = await api("/api/student/learning");
        renderLearning();
      } catch (error) {
        showToast(error.message);
      }
    }
  }

  function renderIndependentPractice() {
    const host = $("#independentPractice");
    if (!host) return;
    if (!currentUser) {
      host.innerHTML = `<p class="empty-state">Дасгал, оноо хадгалахын тулд сурагчийн эрхээр нэвтэрнэ үү.</p>`;
      return;
    }
    if (currentUser.role !== "student") {
      host.innerHTML = `<p class="empty-state">Бие даалтын оролдлого, оноог сурагчийн эрхээр ажиллуулна.</p>`;
      return;
    }
    if (!exercises.length) {
      host.innerHTML = `<p class="empty-state">Энэ бүлэгт бие даан ажиллах нийтлэгдсэн дасгал одоогоор алга.</p>`;
      return;
    }
    const selected = exercises.find((item) => item.id === $("#independentExerciseSelect")?.value) || exercises[0];
    const options = exercises.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selected.id ? "selected" : ""}>${escapeHtml(item.topic)} · ${item.level === "basic" ? "Суурь" : item.level === "standard" ? "Стандарт" : "Ахисан"}</option>`).join("");
    host.innerHTML = `<label class="reference-search">Бодлого сонгох<select id="independentExerciseSelect">${options}</select></label><article class="exercise-card independent-exercise"><div class="exercise-meta"><span>${escapeHtml(selected.topic)}</span><span>${selected.points} оноо</span><span>Шалгуур ${escapeHtml(selected.criteria.join(", "))}</span></div><h4>${escapeHtml(selected.prompt)}</h4><form class="answer-form" id="independentExerciseForm" data-id="${escapeHtml(selected.id)}"><label for="independentAnswer">Хариу</label><input id="independentAnswer" name="answer" required maxlength="1000" autocomplete="off"><button class="filter-button">Шалгах, хадгалах</button></form><div class="answer-feedback" aria-live="polite"></div></article>`;
    $("#independentExerciseSelect").addEventListener("change", renderIndependentPractice);
    $("#independentExerciseForm").addEventListener("submit", submitExercise);
  }

  async function submitLearningQuiz(event) {
    event.preventDefault();
    if (!requireStudent()) return;
    const form = event.target;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const answers = Object.fromEntries(new FormData(form).entries());
      const result = await post(`/api/learning/quizzes/${encodeURIComponent(form.dataset.quiz)}/submit`, { answers });
      learningQuizResults.set(form.dataset.quiz, result);
      learningState = await api("/api/student/learning");
      renderLearning();
      showToast("Мини сорилын хариу, оноо хадгалагдлаа.");
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function submitDailyQuiz(event) {
    event.preventDefault();
    if (!requireStudent()) return;
    const form = event.target;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const result = await post("/api/learning/quizzes/daily/submit", {
        answers: { [form.dataset.question]: new FormData(form).get("answer") }
      });
      dailyResult = result;
      learningState = await api("/api/student/learning");
      renderDailyQuiz();
      showToast("Өдрийн сорилын үр дүн хадгалагдлаа.");
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
    }
  }

  function renderExercises() {
    if (!exercises.length) {
      $("#exerciseList").innerHTML = `<p class="empty-state">Сонгосон түвшинд нийтлэгдсэн дасгал одоогоор алга.</p>`;
      return;
    }
    $("#exerciseList").innerHTML = exercises.map((exercise) => `
      <article class="exercise-card" data-exercise="${escapeHtml(exercise.id)}">
        <div class="exercise-meta"><span>${escapeHtml(exercise.topic)}</span><span class="level-badge ${escapeHtml(exercise.level)}">${{ basic: "Суурь", standard: "Стандарт", advanced: "Ахисан" }[exercise.level]}</span><span>${exercise.points} оноо</span></div>
        <h3>${escapeHtml(exercise.prompt)}</h3>
        <form class="answer-form" data-id="${escapeHtml(exercise.id)}"><label for="answer-${escapeHtml(exercise.id)}">Таны хариу</label><input id="answer-${escapeHtml(exercise.id)}" name="answer" required maxlength="1000" autocomplete="off"><button class="filter-button">Хариуг шалгах</button></form>
        <div class="answer-feedback" aria-live="polite"></div>
      </article>`).join("");
    document.querySelectorAll(".answer-form").forEach((form) => form.addEventListener("submit", submitExercise));
    if ($("#assignExercise")) {
      $("#assignExercise").innerHTML = exercises.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.topic)} · ${escapeHtml(item.level)} · ${escapeHtml(item.id)}</option>`).join("");
    }
    $("#gameExercise")?.replaceChildren(...exercises.map((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.topic} · ${item.level}`;
      return option;
    }));
  }

  async function submitExercise(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const answer = new FormData(form).get("answer");
      const result = await post(`/api/exercises/${encodeURIComponent(form.dataset.id)}/attempts`, { answer });
      form.nextElementSibling.innerHTML = `<strong class="${result.correct ? "correct" : "incorrect"}">${result.correct ? "Зөв!" : "Дахин оролдоорой"}</strong><p>${escapeHtml(result.message)} ${result.points_earned}/${result.points_possible} оноо</p><p>${escapeHtml(result.explanation)}</p><p class="hint">Санамж: ${escapeHtml(result.hint)}</p>`;
      showToast(result.correct ? `Зөв хариуллаа! +${result.points_earned} оноо.` : "Хариулт хадгалагдлаа.");
      const dashboard = await api("/api/student/dashboard");
      renderProgress(dashboard);
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function startAssessment() {
    if (!requireLogin()) return;
    const id = $("#assessmentSelect").value;
    if (!id) return showToast("Шалгалтаа сонгоно уу.");
    try {
      const attempt = await post(`/api/assessments/${encodeURIComponent(id)}/start`, {});
      const details = await api(`/api/assessments/${encodeURIComponent(id)}`);
      renderAssessment(details.assessment, attempt);
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderAssessment(assessment, attempt) {
    window.clearInterval(assessmentTimer);
    const end = new Date(attempt.expires_at).getTime();
    let currentIndex = 0;
    const savedAnswers = {};
    const renderTimer = () => {
      const seconds = Math.max(0, Math.floor((end - Date.now()) / 1000));
      $("#assessmentTimer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
      if (!seconds) {
        window.clearInterval(assessmentTimer);
        $("#assessmentSubmit").disabled = true;
        $("#assessmentNext").disabled = true;
        showToast("Шалгалтын хугацаа дууслаа. Хариултыг илгээх боломжгүй.");
      }
    };
    $("#assessmentWork").innerHTML = `<div class="test-heading"><h3>${escapeHtml(assessment.title)}</h3><strong id="assessmentTimer"></strong></div>
      <form id="assessmentForm" data-attempt-id="${escapeHtml(attempt.attempt_id)}">${assessment.questions.map((question, index) => `<label class="test-question"><strong>${index + 1}. ${escapeHtml(question.prompt)}</strong><input name="${escapeHtml(question.id)}" maxlength="1000" autocomplete="off" aria-label="${index + 1}-р асуултын хариу"></label>`).join("")}
      <button class="primary-button action-button" id="assessmentSubmit">Шалгалт дуусгах, илгээх</button></form><div id="assessmentResult" aria-live="polite"></div>`;
    renderTimer();
    assessmentTimer = window.setInterval(renderTimer, 1000);
    startedAt = Date.now();
    const assessmentForm = $("#assessmentForm");
    const questionLabels = [...assessmentForm.querySelectorAll(".test-question")];
    const questionProgress = document.createElement("div");
    questionProgress.className = "assessment-question-progress";
    assessmentForm.insertBefore(questionProgress, questionLabels[0]);
    const navigation = document.createElement("div");
    navigation.className = "assessment-navigation";
    const previousButton = document.createElement("button");
    previousButton.type = "button";
    previousButton.className = "filter-button";
    previousButton.textContent = "Өмнөх бодлого";
    const nextButton = document.createElement("button");
    nextButton.type = "button";
    nextButton.id = "assessmentNext";
    nextButton.className = "filter-button";
    nextButton.textContent = "Дараагийн бодлого";
    const submitButton = $("#assessmentSubmit");
    navigation.append(previousButton, nextButton);
    assessmentForm.insertBefore(navigation, submitButton);
    let currentQuestion = 0;
    const showQuestion = (index) => {
      currentQuestion = index;
      questionLabels.forEach((label, labelIndex) => label.classList.toggle("hidden", labelIndex !== currentQuestion));
      questionProgress.textContent = `Бодлого ${currentQuestion + 1} / ${questionLabels.length}`;
      previousButton.disabled = currentQuestion === 0;
      nextButton.classList.toggle("hidden", currentQuestion === questionLabels.length - 1);
      submitButton.classList.toggle("hidden", currentQuestion !== questionLabels.length - 1);
    };
    const currentAnswerIsValid = () => {
      const input = questionLabels[currentQuestion]?.querySelector("input");
      return !input || input.reportValidity();
    };
    previousButton.addEventListener("click", () => {
      if (currentQuestion > 0) showQuestion(currentQuestion - 1);
    });
    nextButton.addEventListener("click", () => {
      if (currentAnswerIsValid() && currentQuestion < questionLabels.length - 1) showQuestion(currentQuestion + 1);
    });
    assessmentForm.addEventListener("submit", (event) => {
      if (currentQuestion !== questionLabels.length - 1) {
        event.preventDefault();
        if (currentAnswerIsValid()) showQuestion(currentQuestion + 1);
      }
    });
    assessmentForm.addEventListener("submit", submitAssessment);
    showQuestion(0);
  }

  async function submitAssessment(event) {
    event.preventDefault();
    const answers = Object.fromEntries(new FormData(event.currentTarget).entries());
    const form = event.currentTarget;
    const button = $("#assessmentSubmit");
    button.disabled = true;
    window.clearInterval(assessmentTimer);
    try {
      const result = await post(`/api/assessments/attempts/${encodeURIComponent(form.dataset.attemptId)}/submit`, { answers });
      const byId = new Map(exercises.map((exercise) => [exercise.id, exercise]));
      $("#assessmentResult").innerHTML = `<div class="platform-card"><h3>Үр дүн: ${result.points_earned}/${result.points_possible} оноо · ${result.percentage}%</h3>${Object.entries(result.answers).map(([id, answer]) => {
        const exercise = byId.get(id);
        return `<div class="answer-review"><strong class="${answer.correct ? "correct" : "incorrect"}">${answer.correct ? "Зөв" : "Буруу"}</strong><span>${escapeHtml(exercise?.prompt || id)}</span><p>${escapeHtml(exercise?.explanation || "")}</p></div>`;
      }).join("")}</div>`;
      form.remove();
      const dashboard = await api("/api/student/dashboard");
      renderProgress(dashboard);
      showToast("Шалгалтын үр дүн хадгалагдлаа.");
      await refreshClassroom();
    } catch (error) {
      button.disabled = false;
      showToast(error.message);
    }
  }

  async function reviewStudentAttempt(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const points = Number(form.querySelector('input[type="number"]').value);
    try {
      await post(`/api/teacher/attempts/${encodeURIComponent(form.dataset.attempt)}/review`, { points });
      showToast("Багшийн үнэлгээ хадгалагдлаа.");
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  function printTeacherReport(report) {
    const print = $("#printReport");
    const table = (heading, headers, rows) => `<h2>${escapeHtml(heading)}</h2><table><thead><tr>${headers.map((value) => `<th>${escapeHtml(value)}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.map((row) => `<tr>${row.map((value) => `<td>${escapeHtml(value ?? "—")}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${headers.length}">Мэдээлэл байхгүй</td></tr>`}</tbody></table>`;
    print.innerHTML = `<h1>Tsedo's math · Багшийн тайлан</h1><p>Тайлан гаргасан: ${escapeHtml(currentUser.display_name)} · ${new Date().toLocaleString("mn-MN")}</p>
      ${table("Ангийн дүн", ["Анги", "Сурагч", "Даалгавар", "Дундаж хувь", "Үнэлэх хариулт"], report.classes.map((row) => [row.name, row.student_count, row.assignment_count, row.average_percent == null ? "—" : `${row.average_percent}%`, row.pending_review]))}
      ${table("Сурагчдын ахиц", ["Анги", "Сурагч", "Оролдлого", "Оноо", "Алдсан бодлого"], report.students.map((row) => [row.class_name, row.display_name, row.attempts, row.points, row.error_questions]))}
      ${table("Дасгалын гүйцэтгэл", ["Сурагч", "Сэдэв", "Түвшин", "Хариулт", "Автомат оноо", "Багшийн оноо"], report.attempts.map((row) => [row.display_name, row.topic, row.level, row.answer, `${row.points_earned}/${row.points}`, row.reviewed_points == null ? "Үнэлээгүй" : `${row.reviewed_points}/${row.points}`]))}
      ${table("Шалгалт", ["Сурагч", "Шалгалт", "Оноо", "Боломжит оноо", "Илгээсэн"], report.assessments.map((row) => [row.display_name, row.title, row.points_earned, row.max_points, row.submitted_at]))}`;
    document.body.classList.add("print-report");
    window.addEventListener("afterprint", () => document.body.classList.remove("print-report"), { once: true });
    window.print();
  }

  async function createClass(event) {
    event.preventDefault();
    try {
      const result = await post("/api/classes", { name: $("#newClassName").value });
      newJoinCode = result.class.join_code;
      showToast("Анги үүсгэлээ. Нэг удаа харагдах кодоо хадгална уу.");
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function joinClass(event) {
    event.preventDefault();
    try {
      const result = await post("/api/classes/join", { join_code: $("#joinCode").value });
      showToast(`${result.class.name} ангид амжилттай элссэн.`);
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function createAssignment(event) {
    event.preventDefault();
    try {
      const classId = $("#assignClass").value;
      await post(`/api/classes/${encodeURIComponent(classId)}/assignments`, { exercise_id: $("#assignExercise").value });
      showToast("Дасгалыг ангид даалгалаа.");
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function addStudentToClass(event) {
    event.preventDefault();
    const classId = $("#studentClass").value;
    try {
      const result = await post(`/api/classes/${encodeURIComponent(classId)}/students`, {
        identifier: $("#studentEmail").value
      });
      $("#studentEmail").value = "";
      showToast(`${result.student.display_name} сурагчийг ${result.class.name} ангид нэмлээ.`);
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function removeStudentFromClass(event) {
    const button = event.currentTarget;
    if (!window.confirm("Энэ сурагчийг ангиас хасах уу? Бүртгэл болон сурлагын мэдээлэл устахгүй.")) return;
    try {
      const result = await post(
        `/api/classes/${encodeURIComponent(button.dataset.class)}/students/${encodeURIComponent(button.dataset.student)}/remove`
      );
      showToast(`${result.student.display_name} сурагчийг ${result.class.name} ангиас хаслаа.`);
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function assignAssessment(event) {
    event.preventDefault();
    try {
      await post(`/api/classes/${encodeURIComponent($("#assessmentClass").value)}/assessment-assignments`, {
        assessment_id: $("#assessmentToAssign").value
      });
      showToast("Шалгалтыг ангид оноолоо.");
      await refreshClassroom();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function startGame() {
    const exercise = exercises.find((item) => item.id === $("#gameExercise").value);
    if (!exercise) return;
    startedAt = Date.now();
    $("#gameArea").innerHTML = `<div class="game-prompt"><h4>${escapeHtml(exercise.prompt)}</h4><form id="gameForm" class="answer-form"><label for="gameAnswer">Хариулт</label><input id="gameAnswer" name="answer" required maxlength="1000"><button class="filter-button">Илгээх</button></form><div id="gameResult"></div></div>`;
    $("#gameForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const result = await post("/api/games/quiz", {
          exercise_id: exercise.id,
          answer: new FormData(event.currentTarget).get("answer"),
          elapsed_seconds: Math.floor((Date.now() - startedAt) / 1000)
        });
        $("#gameResult").innerHTML = `<p class="${result.correct ? "correct" : "incorrect"}">${result.correct ? "Зөв хариулт!" : "Дараагийн удаа амжилт хүсье."} · +${result.points_earned} оноо${result.bonus_points ? ` · хурдны нэмэлт +${result.bonus_points}` : ""}</p>`;
        renderProgress(await api("/api/student/dashboard"));
        await refreshLeaderboard();
      } catch (error) {
        showToast(error.message);
      }
    });
  }

  async function refreshLeaderboard() {
    if (currentUser?.role !== "student") return;
    try {
      const { leaderboard } = await api("/api/leaderboard");
      $("#leaderboard").innerHTML = `<h4>Онооны жагсаалт</h4>${leaderboard.slice(0, 10).map((person, index) => `<div class="leader-row"><span>${index + 1}. ${escapeHtml(person.display_name)}</span><strong>${person.points} оноо</strong></div>`).join("") || "<p>Оноо бүртгэгдээгүй байна.</p>"}`;
    } catch (error) {
      showToast(error.message);
    }
  }

  async function refreshSession() {
    const result = await api("/api/session");
    currentUser = result.user;
    renderProfile();
    await refreshData();
    await refreshLearningState();
  }

  chapterGrid.addEventListener("click", async (event) => {
    const startGameButton = event.target.closest("[data-start-chapter-game]");
    if (startGameButton) {
      await startChapterGame(startGameButton.dataset.startChapterGame);
      return;
    }
    const nextGameButton = event.target.closest("[data-next-chapter-game]");
    if (nextGameButton) {
      advanceChapterGame(nextGameButton.dataset.nextChapterGame);
      return;
    }
    const summary = event.target.closest(".chapter-summary");
    if (!summary) return;
    const card = summary.closest(".chapter-card");
    const expanded = card.classList.toggle("expanded");
    summary.setAttribute("aria-expanded", String(expanded));
  });
  chapterGrid.addEventListener("submit", (event) => {
    if (!event.target.matches(".chapter-game-form")) return;
    submitChapterGame(event);
  });
  $("#searchInput").addEventListener("input", renderChapters);
  $("#themeToggle").addEventListener("click", () => {
    const theme = document.body.classList.contains("dark-mode") ? "light" : "dark";
    localStorage.setItem("dashboard-theme", theme);
    applyTheme(theme);
  });
  $("#starFilter").addEventListener("click", () => {
    filterStarred = !filterStarred;
    $("#starFilter").classList.toggle("selected", filterStarred);
    $("#starFilter").setAttribute("aria-pressed", String(filterStarred));
    renderChapters();
  });
  $("#clearFilter").addEventListener("click", () => {
    filterStarred = false;
    $("#starFilter").classList.remove("selected");
    $("#starFilter").setAttribute("aria-pressed", "false");
    $("#searchInput").value = "";
    renderChapters();
  });
  $("#profileButton").addEventListener("click", () => {
    if (!currentUser) return openAuth();
    const profileDialog = $("#profileSettingsDialog");
    if (currentUser.profile_image) $("#profilePhotoPreview").src = currentUser.profile_image;
    else $("#profilePhotoPreview").removeAttribute("src");
    $("#newProfileName").value = currentUser.display_name;
    $("#nameChangePassword").value = "";
    $("#newProfileEmail").value = currentUser.email;
    $("#emailChangePassword").value = "";
    $("#currentPassword").value = "";
    $("#newProfilePassword").value = "";
    $("#confirmProfilePassword").value = "";
    profileDialog.showModal();
  });
  $("#changeLogoButton").addEventListener("click", () => $("#logoImageInput").click());
  $("#changeCoverButton").addEventListener("click", () => $("#coverImageInput").click());
  $("#coverImageInput").addEventListener("change", async (event) => {
    const input = event.currentTarget;
    const file = input.files[0];
    if (!file) return;
    if (currentUser?.role !== "teacher") {
      showToast("Нүүр зураг солих эрх зөвхөн багшид байна.");
      input.value = "";
      return;
    }
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      showToast("PNG, JPEG эсвэл WebP зураг сонгоно уу.");
      input.value = "";
      return;
    }
    if (file.size > 4 * 1024 * 1024) {
      showToast("Нүүр зургийн хэмжээ 4 MB-аас хэтрэхгүй байна.");
      input.value = "";
      return;
    }
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener("load", () => resolve(reader.result));
        reader.addEventListener("error", () => reject(new Error("Зургийн файлыг уншиж чадсангүй.")));
        reader.readAsDataURL(file);
      });
      const result = await post("/api/site/cover", { data_url: dataUrl });
      applySiteCover(result.cover_image);
      showToast("Нүүр зургийг сольж хадгаллаа.");
    } catch (error) {
      showToast(error.message);
    } finally {
      input.value = "";
    }
  });
  $("#logoImageInput").addEventListener("change", async (event) => {
    const input = event.currentTarget;
    const file = input.files[0];
    if (!file) return;
    if (currentUser?.role !== "teacher") {
      showToast("Лого зураг солих эрх зөвхөн багшид байна.");
      input.value = "";
      return;
    }
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      showToast("PNG, JPEG эсвэл WebP зураг сонгоно уу.");
      input.value = "";
      return;
    }
    if (file.size > 4 * 1024 * 1024) {
      showToast("Лого зургийн хэмжээ 4 MB-аас хэтрэхгүй байна.");
      input.value = "";
      return;
    }
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener("load", () => resolve(reader.result));
        reader.addEventListener("error", () => reject(new Error("Зургийн файлыг уншиж чадсангүй.")));
        reader.readAsDataURL(file);
      });
      const result = await post("/api/site/logo", { data_url: dataUrl });
      $("#brandLogoImage").src = result.logo_image;
      $("#brandLogoImage").classList.remove("hidden");
      $("#brandMarkFallback").classList.add("hidden");
      showToast("TSEDO'S MATH лого зураг солигдлоо.");
    } catch (error) {
      showToast(error.message);
    } finally {
      input.value = "";
    }
  });
  $("#closeProfileSettings").addEventListener("click", () => $("#profileSettingsDialog").close());
  $("#profilePhotoInput").addEventListener("change", async (event) => {
    const input = event.currentTarget;
    const file = input.files[0];
    if (!file) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      showToast("PNG, JPEG эсвэл WebP зураг сонгоно уу.");
      input.value = "";
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      showToast("Зургийн хэмжээ 2 MB-аас хэтрэхгүй байна.");
      input.value = "";
      return;
    }
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener("load", () => resolve(reader.result));
        reader.addEventListener("error", () => reject(new Error("Зургийн файлыг уншиж чадсангүй.")));
        reader.readAsDataURL(file);
      });
      const result = await post("/api/profile/photo", { data_url: dataUrl });
      currentUser = result.user;
      renderProfile();
      $("#profilePhotoPreview").src = currentUser.profile_image;
      showToast("Профайлын зураг хадгалагдлаа.");
    } catch (error) {
      showToast(error.message);
    } finally {
      input.value = "";
    }
  });
  $("#changeProfileNameForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await post("/api/profile/name", {
        display_name: $("#newProfileName").value,
        password: $("#nameChangePassword").value
      });
      currentUser = result.user;
      renderProfile();
      $("#newProfileName").value = currentUser.display_name;
      $("#nameChangePassword").value = "";
      showToast("Нэр амжилттай солигдлоо.");
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#changeEmailForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await post("/api/profile/email", {
        email: $("#newProfileEmail").value,
        password: $("#emailChangePassword").value
      });
      currentUser = result.user;
      renderProfile();
      $("#emailChangePassword").value = "";
      $("#newProfileEmail").value = currentUser.email;
      showToast("Имэйл хаяг амжилттай солигдлоо.");
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#changePasswordForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("#newProfilePassword").value !== $("#confirmProfilePassword").value) {
      showToast("Шинэ нууц үгийн давталт таарахгүй байна.");
      return;
    }
    try {
      await post("/api/profile/password", {
        current_password: $("#currentPassword").value,
        new_password: $("#newProfilePassword").value
      });
      $("#currentPassword").value = "";
      $("#newProfilePassword").value = "";
      $("#confirmProfilePassword").value = "";
      showToast("Нууц үг солигдлоо. Бусад төхөөрөмжөөс гаргалаа.");
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#loginButton").addEventListener("click", async () => {
    if (!currentUser) return openAuth();
    try {
      await post("/api/auth/logout", {});
      currentUser = null;
      renderProfile();
      await refreshData();
      await refreshLearningState();
      showToast("Системээс гарлаа.");
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#closeAuth").addEventListener("click", () => dialog.close());
  $("#toggleRegister").addEventListener("click", () => setAuthMode($("#registerFields").classList.contains("hidden")));
  $("#authRole").addEventListener("change", () => $("#teacherCodeField").classList.toggle("hidden", $("#authRole").value !== "teacher"));
  $("#authForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const register = !$("#registerFields").classList.contains("hidden");
    const identifier = $("#authEmail").value.trim();
    const body = {
      email: identifier.includes("@") ? identifier : "",
      phone: identifier.includes("@") ? "" : identifier,
      password: $("#authPassword").value
    };
    if (register) {
      body.display_name = $("#authName").value;
      body.role = $("#authRole").value;
      body.teacher_invite_code = $("#teacherCode").value;
    }
    try {
      await post(register ? "/api/auth/register" : "/api/auth/login", body);
      dialog.close();
      await refreshSession();
      showToast("Амжилттай нэвтэрлээ.");
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#showReset").addEventListener("click", () => {
    $("#resetEmail").value = $("#authEmail").value;
    $("#authForm").classList.add("hidden");
    $("#resetForm").classList.remove("hidden");
  });
  $("#requestReset").addEventListener("click", async () => {
    try {
      await post("/api/auth/password-reset/request", { email: $("#resetEmail").value });
      showToast("Боломжтой бол сэргээх заавар илгээлээ. Хөгжүүлэлтийн горимд серверийн консолыг шалгана уу.");
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#confirmReset").addEventListener("click", async () => {
    try {
      await post("/api/auth/password-reset/confirm", {
        email: $("#resetEmail").value, token: $("#resetToken").value, new_password: $("#newPassword").value
      });
      showToast("Нууц үг шинэчлэгдлээ. Шинэ нууц үгээр нэвтэрнэ үү.");
      setAuthMode(false);
    } catch (error) {
      showToast(error.message);
    }
  });
  $("#exerciseLevel").addEventListener("change", () => refreshData().catch((error) => showToast(error.message)));
  $("#exerciseChapter").addEventListener("change", () => refreshData().catch((error) => showToast(error.message)));
  $("#startAssessment").addEventListener("click", startAssessment);
  $("#eeshQuizContent").addEventListener("click", (event) => {
    if (event.target.closest("#startEeshQuiz")) startEeshQuiz();
    const materialButton = event.target.closest("[data-start-eesh-material]");
    if (materialButton) startEeshMaterialPractice(materialButton.dataset.startEeshMaterial);
    const stepButton = event.target.closest("[data-eesh-step]");
    if (stepButton) {
      const step = Number(stepButton.dataset.eeshStep);
      persistEeshMaterialResponse().then((saved) => {
        if (!saved) return;
        activeEeshSlide = Math.min(
          activeEeshMaterial.slide_count,
          Math.max(1, activeEeshSlide + step)
        );
        renderEeshMaterialViewer();
      });
    }
    if (event.target.closest("[data-close-eesh-viewer]")) {
      persistEeshMaterialResponse().then((saved) => {
        if (!saved) return;
        activeEeshMaterial = null;
        $("#eeshMaterialWork").innerHTML = "";
      });
    }
  });
  $("#learningContent").addEventListener("click", async (event) => {
    const deleteMediaButton = event.target.closest("[data-delete-lesson-media]");
    if (deleteMediaButton) {
      if (!window.confirm("Энэ нэмэлт материалыг устгах уу?")) return;
      deleteMediaButton.disabled = true;
      try {
        await api(`/api/learning/media/${encodeURIComponent(deleteMediaButton.dataset.deleteLessonMedia)}`, {
          method: "DELETE",
        });
        await loadLearning();
        showToast("Нэмэлт материал устгагдлаа.");
      } catch (error) {
        deleteMediaButton.disabled = false;
        showToast(error.message);
      }
      return;
    }
    const retryButton = event.target.closest(".retry-learning-quiz");
    if (retryButton) {
      await openLesson(retryButton.dataset.retryLesson, false);
      const input = document.querySelector(
        `form.learning-quiz-form input[name="${CSS.escape(retryButton.dataset.retryQuestion)}"]`
      );
      input?.scrollIntoView({ behavior: "smooth", block: "center" });
      input?.focus();
      return;
    }
    const retryExercise = event.target.closest(".retry-learning-exercise");
    if (retryExercise) {
      $("#exerciseLevel").value = "";
      try {
        await refreshData();
        const form = document.querySelector(`.answer-form[data-id="${CSS.escape(retryExercise.dataset.exercise)}"]`);
        form?.scrollIntoView({ behavior: "smooth", block: "center" });
        form?.querySelector("input").focus();
      } catch (error) {
        showToast(error.message);
      }
      return;
    }
    const lessonButton = event.target.closest("[data-learning-lesson]");
    if (lessonButton) {
      await openLesson(lessonButton.dataset.learningLesson);
      return;
    }
    const chapterButton = event.target.closest("[data-learning-chapter]");
    if (chapterButton) {
      selectedLearningChapter = chapterButton.dataset.learningChapter;
      activeLessonId = null;
      renderLearning();
      return;
    }
    if (event.target.closest(".exam-prep-start")) {
      if (!requireLogin()) return;
      $("#assessmentSelect").value = "absolute-unit-test";
      $("#tests").scrollIntoView({ behavior: "smooth" });
      await startAssessment();
    }
  });
  $("#learning").addEventListener("input", (event) => {
    if (event.target.id === "graphCenter" || event.target.id === "graphRadius") updateGraph();
    if (event.target.id === "formulaSearch") {
      const query = event.target.value.trim().toLocaleLowerCase("mn");
      document.querySelectorAll(".searchable-formula").forEach((item) => {
        item.classList.toggle("hidden", !item.dataset.search.includes(query));
      });
    }
    if (event.target.id === "glossarySearch") {
      const query = event.target.value.trim().toLocaleLowerCase("mn");
      document.querySelectorAll(".searchable-term").forEach((item) => {
        item.classList.toggle("hidden", !item.dataset.search.includes(query));
      });
    }
  });
  $("#learningContent").addEventListener("submit", (event) => {
    if (event.target.matches(".learning-quiz-form")) submitLearningQuiz(event);
    if (event.target.matches(".daily-quiz-form")) submitDailyQuiz(event);
    if (event.target.matches(".lesson-media-upload")) uploadLessonMedia(event);
  });
  $("#menuToggle").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => $("#sidebar").classList.remove("open")));

  renderStats();
  renderChapters();
  renderCriteria();
  renderProgress();
  renderProfile();
  applyTheme(localStorage.getItem("dashboard-theme") || "light");
  loadSiteLogo().catch((error) => {
    console.error("Лого зургийг ачаалж чадсангүй.", error);
    showToast("Лого зургийг ачаалж чадсангүй. Хуудсаа дахин ачаална уу.");
  });
  loadSiteCover().catch((error) => {
    console.error("Нүүр зургийг ачаалж чадсангүй.", error);
    showToast("Нүүр зургийг ачаалж чадсангүй. Хуудсаа дахин ачаална уу.");
  });
  loadLearning().catch((error) => {
    console.error("Цахим хичээлийг ачаалж чадсангүй.", error);
    $("#learningContent").innerHTML = `<p class="empty-state">Цахим хичээлийг ачаалж чадсангүй. Серверээ шалгаад дахин ачаална уу.</p>`;
  });
  loadEeshQuizzes();
  refreshSession().catch((error) => {
    console.error("Сервертэй холбогдож хэрэглэгчийн сессийг уншиж чадсангүй.", error);
    showToast("Серверт холбогдож чадсангүй. README-ийн дагуу серверээ эхлүүлнэ үү.");
  });
})();
