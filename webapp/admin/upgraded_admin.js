/* =========================================================
   YOUR SHOP - ADMIN PANEL
   Version: HTML-compatible unified build
   Stage: 11.4 Orders + Order Details
   ========================================================= */

"use strict";

const API = "/api/admin";

let currentProducts = [];
let currentGames = [];
let currentCategories = [];
let currentInventory = [];
let selectedInventoryIds = new Set();
let selectedGameIds = new Set();
let selectedCategoryIds = new Set();
let selectedProductIds = new Set();
let selectedPromoIds = new Set();
let currentOrders = [];
let currentPayments = [];
let currentUsers = [];

const $ = (id) => document.getElementById(id);

function adminImageMarkup(src, alt = "") {
    const value = String(src || "").trim();
    const safeAlt = escapeHTML(alt);
    if (!value) return `<div class="admin-image-placeholder">🖼️</div>`;
    return `<img src="${escapeHTML(value)}" alt="${safeAlt}" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement("div"),{className:"admin-image-placeholder",textContent:"🖼️"}))">`;
}

function escapeHTML(value) {
    if (value === null || value === undefined) return "";
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function normalizeList(data, keys = []) {
    if (Array.isArray(data)) return data;
    for (const key of keys) {
        if (Array.isArray(data?.[key])) return data[key];
    }
    return [];
}

async function apiFetch(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);

    try {
        const fetchOptions = {
            credentials: "include",
            cache: "no-store",
            signal: controller.signal,
            ...options,
            headers: {
                Accept: "application/json",
                ...(options.body
                    ? { "Content-Type": "application/json" }
                    : {}),
                ...(options.headers || {})
            }
        };

        const response = await fetch(url, fetchOptions);

        let data = {};
        const text = await response.text();

        if (text) {
            try {
                data = JSON.parse(text);
            } catch {
                data = { raw: text };
            }
        }

        if (!response.ok) {
            throw new Error(
                data?.error ||
                data?.message ||
                `Request failed (${response.status})`
            );
        }

        return data;
    } catch (error) {
        if (error.name === "AbortError") {
            throw new Error("Request timed out.");
        }

        throw error;
    } finally {
        clearTimeout(timeout);
    }
}

function setStatus(text, type = "") {
    const el = $("statusBadge");
    if (!el) return;

    el.textContent = text;
    el.className = `status ${type}`.trim();
}

function showMessage(message, type = "success") {
    const box = $("globalMessage");

    if (!box) {
        console.log(`[ADMIN ${type}]`, message);
        return;
    }

    box.textContent = message;
    box.className = `global-message ${type}`;
    box.style.display = "block";

    clearTimeout(showMessage._timer);

    showMessage._timer = setTimeout(() => {
        box.style.display = "none";
    }, 3500);
}

function setContainerLoading(id, text = "Loading…") {
    const el = $(id);

    if (el) {
        el.innerHTML = `
            <div class="empty">
                ${escapeHTML(text)}
            </div>
        `;
    }
}

function closeModal(id) {
    const modal = $(id);
    if (!modal) return;

    modal.classList.remove("active");
    modal.style.display = "none";

    const backdrop = $("modalBackdrop");

    if (backdrop) {
        backdrop.classList.remove("active");
        backdrop.style.display = "none";
    }
}

function openModal(id) {
    const modal = $(id);
    if (!modal) return;

    modal.classList.add("active");
    modal.style.display = "flex";

    const backdrop = $("modalBackdrop");

    if (backdrop) {
        backdrop.classList.add("active");
        backdrop.style.display = "block";
    }
}

function backdropClose(event) {
    if (event.target !== event.currentTarget) return;

    document.querySelectorAll(".modal.active").forEach((modal) => {
        closeModal(modal.id);
    });
}

function findProduct(id) {
    return currentProducts.find(
        (item) => Number(item.id) === Number(id)
    );
}

function findGame(id) {
    return currentGames.find(
        (item) => Number(item.id) === Number(id)
    );
}

function findCategory(id) {
    return currentCategories.find(
        (item) => Number(item.id) === Number(id)
    );
}

function productIsActive(product) {
    return product?.active === true || product?.active === 1;
}

function productIsFeatured(product) {
    return product?.featured === true || product?.featured === 1;
}

function normalizeImageUrls(value) {
    return String(value || "")
        .split(/\r?\n/)
        .map((x) => x.trim())
        .filter(Boolean)
        .filter((x, index, arr) => arr.indexOf(x) === index);
}

function adminImageSrc(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    if (/^https?:\/\//i.test(raw) || raw.startsWith("/")) {
        return raw;
    }
    if (/^tg:/i.test(raw)) {
        return `/media/telegram/${encodeURIComponent(raw.slice(3))}`;
    }
    return `/media/telegram/${encodeURIComponent(raw)}`;
}

function formatDate(value) {
    if (!value) return "-";

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value);
        }

        return date.toLocaleString();
    } catch {
        return String(value);
    }
}

function formatStars(value) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
        return "0";
    }

    return number.toLocaleString();
}

function statusClass(status) {
    const normalized = String(status || "").toLowerCase();

    if (
        normalized === "paid" ||
        normalized === "completed" ||
        normalized === "delivered" ||
        normalized === "success" ||
        normalized === "successful"
    ) {
        return "good";
    }

    if (
        normalized === "cancelled" ||
        normalized === "canceled" ||
        normalized === "failed" ||
        normalized === "refunded"
    ) {
        return "bad";
    }

    return "warning";
}

/* =========================================================
   AUTH
   ========================================================= */

async function checkAuth() {
    try {
        setStatus("Checking session…");

        const data = await apiFetch(`${API}/me`);

        console.log("[ADMIN] /me:", data);

        const authenticated =
            data?.authenticated === true ||
            data?.authenticated === 1 ||
            data?.ok === true ||
            data?.admin === true ||
            data?.is_admin === true;

        if (!authenticated) {
            setStatus("Not authenticated", "danger");
            return false;
        }

        setStatus("Session active", "success");
        return true;
    } catch (error) {
        console.error("[ADMIN] Auth error:", error);

        setStatus("Session error", "danger");

        showMessage(
            `Authentication error: ${error.message}`,
            "error"
        );

        return false;
    }
}

async function logout() {
    try {
        await apiFetch(`${API}/logout`, {
            method: "POST"
        });
    } catch (error) {
        console.error("[ADMIN] Logout error:", error);
    } finally {
        window.location.href = "/admin";
    }
}

/* =========================================================
   NAVIGATION
   ========================================================= */

const sectionInfo = {
    dashboard: ["Dashboard", "Store overview"],
    games: ["Games", "Manage games"],
    categories: ["Categories", "Manage categories"],
    products: ["Products", "Manage products"],
    inventory: ["Inventory", "Manage delivery inventory"],
    analytics: ["Analytics", "Sales and operational reporting"],
    orders: ["Orders", "View customer orders"],
    "test-order": ["Test Store", "Create safe admin test orders"],
    referral: ["Referral Settings", "Configure referral rewards"],
    payments: ["Payments", "View successful payments"],
    users: ["Users", "View registered users"]
};

function switchSection(name) {
    document.querySelectorAll(".section").forEach((section) => {
        section.classList.remove("active");
    });

    const target = $(`section-${name}`);

    if (target) {
        target.classList.add("active");
    }

    document.querySelectorAll("[data-section]").forEach((button) => {
        button.classList.toggle(
            "active",
            button.dataset.section === name
        );
    });

    const info = sectionInfo[name] || sectionInfo.dashboard;

    const title = $("pageTitle");
    const subtitle = $("pageSubtitle");

    if (title) {
        title.textContent = info[0];
    }

    if (subtitle) {
        subtitle.textContent = info[1];
    }

    if (name === "dashboard") {
        loadDashboard();
    }

    if (name === "games") {
        loadGames();
    }

    if (name === "categories") {
        loadCategories();
    }

    if (name === "products") {
        loadProducts();
    }

    if (name === "inventory") {
        populateInventoryProductSelect();
        loadInventory();
    }

    if (name === "analytics") {
        loadAnalytics();
    }

    if (name === "orders") {
        loadOrders();
    }

    if (name === "test-order") {
        loadProducts().then(populateTestOrderProducts);
    }

    if (name === "referral") {
        loadReferralSettings();
    }

    if (name === "payments") {
        loadPayments();
    }

    if (name === "users") {
        loadUsers();
    }
}

function setupNavigation() {
    document.querySelectorAll("[data-section]").forEach((button) => {
        button.addEventListener("click", () => {
            switchSection(button.dataset.section);
        });
    });

    const logoutBtn = $("logoutBtn");

    if (logoutBtn) {
        logoutBtn.addEventListener("click", logout);
    }
}

/* =========================================================
   DASHBOARD
   ========================================================= */

async function loadStoreStatus() {
    const onlineBtn = $("storeStatusOnline");
    const offlineBtn = $("storeStatusOffline");
    const legacyBtn = $("storeStatusToggle");
    const text = $("storeStatusText");
    if (!onlineBtn && !offlineBtn && !legacyBtn) return;
    try {
        const data = await apiFetch(`${API}/store/status`);
        const online = data.online !== false;
        if (onlineBtn) {
            onlineBtn.className = `btn ${online ? "success" : "ghost"}`;
            onlineBtn.disabled = online;
            onlineBtn.textContent = online ? "🟢 Store Online" : "🟢 Turn Store ON";
        }
        if (offlineBtn) {
            offlineBtn.className = `btn ${online ? "ghost" : "danger"}`;
            offlineBtn.disabled = !online;
            offlineBtn.textContent = online ? "🔴 Turn Store OFF" : "🔴 Store Offline";
        }
        if (legacyBtn) {
            legacyBtn.textContent = online ? "🟢 Online" : "🔴 Offline";
            legacyBtn.className = `btn ${online ? "success" : "danger"}`;
            legacyBtn.dataset.online = online ? "true" : "false";
        }
        if (text) text.textContent = online
            ? "The shop is online. Customers can browse, order and pay."
            : "The shop is offline. Customers receive “Store is currently offline” and customer actions are blocked.";
    } catch (e) {
        if (text) text.textContent = e.message;
    }
}

async function setStoreStatus(online) {
    try {
        await apiFetch(`${API}/store/status`, {
            method: "POST",
            body: JSON.stringify({online})
        });
        await loadStoreStatus();
        showMessage(online ? "Store is now ONLINE." : "Store is now OFFLINE.", "success");
    } catch (e) {
        showMessage(`Store status error: ${e.message}`, "error");
    }
}

async function toggleStoreStatus() {
    const legacyBtn = $("storeStatusToggle");
    const currentOnline = legacyBtn?.dataset.online !== "false";
    await setStoreStatus(!currentOnline);
}

async function loadDashboard() {
    try {
        await loadStoreStatus();
        const data = await apiFetch(`${API}/stats`);
        const stats = data?.stats || data || {};

        if ($("statProducts")) {
            $("statProducts").textContent =
                stats.total_products ??
                stats.products ??
                0;
        }

        if ($("statGames")) {
            $("statGames").textContent =
                stats.total_games ??
                stats.games ??
                0;
        }

        if ($("statCategories")) {
            $("statCategories").textContent =
                stats.total_categories ??
                stats.categories ??
                0;
        }

        if ($("statUsers")) {
            $("statUsers").textContent =
                stats.total_users ??
                stats.users ??
                0;
        }

        if ($("statOrders")) {
            $("statOrders").textContent =
                stats.total_orders ??
                stats.orders ??
                0;
        }

        if ($("statRevenue")) {
            $("statRevenue").textContent =
                stats.revenue_stars ??
                stats.total_revenue ??
                stats.total_paid_stars ??
                0;
        }

        await loadRecentOrders();
        renderLowStock();
    } catch (error) {
        console.error("[ADMIN] Dashboard error:", error);

        showMessage(
            `Dashboard error: ${error.message}`,
            "error"
        );
    }
}

async function loadAnalytics() {
    const days = Number($("analyticsDays")?.value || 30);
    try {
        const data = await apiFetch(`${API}/analytics?days=${days}`);
        const summary = data.summary || {};
        if ($("analyticsOrders")) $("analyticsOrders").textContent = Number(summary.orders || 0);
        if ($("analyticsPaidOrders")) $("analyticsPaidOrders").textContent = Number(summary.paid_orders || 0);
        if ($("analyticsRevenue")) $("analyticsRevenue").textContent = Number(summary.revenue_stars || 0);
        const top = data.top_products || [];
        const topBox = $("analyticsTopProducts");
        if (topBox) topBox.innerHTML = top.length
            ? `<table><thead><tr><th>Product</th><th>Units</th><th>Sales</th></tr></thead><tbody>${top.map((row) => `<tr><td>${escapeHTML(row.name || "-")}</td><td>${Number(row.units || 0)}</td><td>⭐ ${Number(row.sales_stars || 0)}</td></tr>`).join("")}</tbody></table>`
            : `<div class="empty">No paid sales in this period.</div>`;
        const daily = data.daily || [];
        const dailyBox = $("analyticsDaily");
        if (dailyBox) dailyBox.innerHTML = daily.length
            ? `<table><thead><tr><th>Day</th><th>Paid orders</th><th>Revenue</th></tr></thead><tbody>${daily.map((row) => `<tr><td>${escapeHTML(row.day || "-")}</td><td>${Number(row.paid_orders || 0)}</td><td>⭐ ${Number(row.revenue_stars || 0)}</td></tr>`).join("")}</tbody></table>`
            : `<div class="empty">No data for this period.</div>`;
    } catch (error) {
        showMessage(`Analytics error: ${error.message}`, "error");
    }
}

function exportInventory(status = "available") {
    window.location.href = `${API}/inventory/export?status=${encodeURIComponent(status)}`;
}

async function loadRecentOrders() {
    const box = $("recentOrders");

    if (!box) return;

    try {
        const data = await apiFetch(`${API}/orders`);

        currentOrders = normalizeList(
            data,
            ["orders", "items"]
        );

        const recent = currentOrders.slice(0, 5);

        if (!recent.length) {
            box.innerHTML = `
                <div class="empty">
                    No orders available
                </div>
            `;
            return;
        }

        box.innerHTML = `
            <div class="mini-orders">
                ${recent.map((order) => `
                    <div class="mini-order">
                        <div>
                            <strong>
                                #${escapeHTML(order.id)}
                            </strong>
                            <span>
                                ${escapeHTML(
                                    order.product_name ||
                                    order.product ||
                                    "Product"
                                )}
                            </span>
                        </div>

                        <div>
                            <strong>
                                ⭐ ${escapeHTML(
                                    order.total_stars ??
                                    order.amount_stars ??
                                    order.price_stars ??
                                    0
                                )}
                            </strong>

                            <span>
                                ${escapeHTML(
                                    order.status ||
                                    "pending"
                                )}
                            </span>
                        </div>
                    </div>
                `).join("")}
            </div>
        `;
    } catch (error) {
        console.error(
            "[ADMIN] Recent orders error:",
            error
        );

        box.innerHTML = `
            <div class="empty error">
                ${escapeHTML(error.message)}
            </div>
        `;
    }
}

/* =========================================================
   GAMES
   ========================================================= */


/* =========================================================
   GAMES
   ========================================================= */

async function loadGames() {
    const box = $("gamesTable");
    setContainerLoading("gamesTable");

    try {
        const data = await apiFetch(`${API}/games`);
        currentGames = normalizeList(data, ["games", "items"]);
        renderGames();
        populateGameSelects();
        populateProductGameFilter();
        populateCategoryGameFilter();
        populateInventoryProductSelect();
    } catch (error) {
        console.error("[ADMIN] Games error:", error);
        if (box) {
            box.innerHTML = `
                <div class="empty error">
                    ${escapeHTML(error.message)}
                </div>
            `;
        }
    }
}

function renderGames() {
    const box = $("gamesTable");
    if (!box) return;
    if (!currentGames.length) {
        box.innerHTML = `<div class="empty">No games available.</div>`;
        return;
    }
    box.innerHTML = `
      <div class="bulk-toolbar">
        <label><input type="checkbox" id="selectAllGames"> Select all</label>
        <span id="gamesSelectedCount">0 selected</span>
        <button class="btn tiny danger" id="bulkDeleteGames" disabled>Delete selected</button>
      </div>
      <div class="admin-card-grid">
        ${currentGames.map(game => `
          <article class="admin-card-item">
            <div class="admin-card-check"><input class="game-check" type="checkbox" data-id="${escapeHTML(game.id)}"></div>
            <div class="admin-card-media">${adminImageMarkup(game.image, game.name)}</div>
            <div class="admin-card-content">
              <div class="card-kicker">GAME #${escapeHTML(game.id)}</div>
              <h3>${escapeHTML(game.name || "-")}</h3>
              <p>${escapeHTML(game.description || "No description.")}</p>
              <div class="card-meta">${game.active ? "🟢 Active" : "🔴 Inactive"} · Sort ${escapeHTML(game.sort_order ?? 0)}</div>
              <div class="card-actions">
                <button class="btn tiny" type="button" data-game-edit="${escapeHTML(game.id)}">Edit</button>
                <button class="btn tiny" type="button" data-game-toggle="${escapeHTML(game.id)}">${game.active ? "Disable" : "Enable"}</button>
                <button class="btn tiny danger" type="button" data-game-delete="${escapeHTML(game.id)}">Delete</button>
              </div>
            </div>
          </article>
        `).join("")}
      </div>`;
    const update = () => {
        selectedGameIds = new Set([...document.querySelectorAll(".game-check:checked")].map(x => Number(x.dataset.id)));
        $("gamesSelectedCount").textContent = `${selectedGameIds.size} selected`;
        $("bulkDeleteGames").disabled = !selectedGameIds.size;
    };
    document.querySelectorAll(".game-check").forEach(x => x.addEventListener("change", update));
    $("selectAllGames")?.addEventListener("change", e => {
        document.querySelectorAll(".game-check").forEach(x => x.checked = e.target.checked);
        update();
    });
    $("bulkDeleteGames")?.addEventListener("click", async () => {
        if (!selectedGameIds.size || !confirm(`Delete ${selectedGameIds.size} games?`)) return;
        try {
            await apiFetch(`${API}/bulk-delete/games`, {method:"POST", body:JSON.stringify({ids:[...selectedGameIds]})});
            selectedGameIds.clear();
            await loadGames();
            await loadCategories();
        } catch (e) { alert(e.message); }
    });
    document.querySelectorAll("[data-game-edit]").forEach(b => b.addEventListener("click", () => editGame(Number(b.dataset.gameEdit))));
    document.querySelectorAll("[data-game-toggle]").forEach(b => b.addEventListener("click", async () => {
        try { await apiFetch(`${API}/games/${b.dataset.gameToggle}/toggle`, {method:"POST"}); await loadGames(); }
        catch(e){ alert(e.message); }
    }));
    document.querySelectorAll("[data-game-delete]").forEach(b => b.addEventListener("click", async () => {
        if (!confirm("Delete this game and its categories?")) return;
        try { await apiFetch(`${API}/games/${b.dataset.gameDelete}`, {method:"DELETE"}); await loadGames(); await loadCategories(); await loadProducts(); }
        catch(e){ alert(e.message); }
    }));
}

function populateGameSelects() {
    const selectIds = ["categoryGame", "productGame"];

    selectIds.forEach((id) => {
        const select = $(id);
        if (!select) return;

        const firstText =
            id === "categoryGame" ? "Select game" : "Select game";

        select.innerHTML = `<option value="">${firstText}</option>`;

        currentGames.forEach((game) => {
            const option = document.createElement("option");
            option.value = game.id;
            option.textContent =
                game.name || `Game ${game.id}`;
            select.appendChild(option);
        });
    });
}

/* =========================================================
   CATEGORIES
   ========================================================= */

async function loadCategories() {
    const box = $("categoriesTable");
    setContainerLoading("categoriesTable");

    try {
        const gameId = $("categoryGameFilter")?.value || "";
        let url = `${API}/categories`;

        // Backend currently exposes /api/admin/categories without
        // a required filter. Fetch all and filter client-side.
        const data = await apiFetch(url);
        currentCategories = normalizeList(data, ["categories", "items"]);

        if (gameId) {
            currentCategories = currentCategories.filter(
                (category) =>
                    String(category.game_id ?? "") === String(gameId)
            );
        }

        renderCategories();
        populateCategorySelect();
        populateProductCategoryFilter();
    } catch (error) {
        console.error("[ADMIN] Categories error:", error);
        if (box) {
            box.innerHTML = `
                <div class="empty error">
                    ${escapeHTML(error.message)}
                </div>
            `;
        }
    }
}

function renderCategories() {
    const box = $("categoriesTable");
    if (!box) return;
    if (!currentCategories.length) {
        box.innerHTML = `<div class="empty">No categories available.</div>`;
        return;
    }
    box.innerHTML = `
      <div class="bulk-toolbar">
        <label><input type="checkbox" id="selectAllCategories"> Select all</label>
        <span id="categoriesSelectedCount">0 selected</span>
        <button class="btn tiny danger" id="bulkDeleteCategories" disabled>Delete selected</button>
      </div>
      <div class="admin-card-grid">
        ${currentCategories.map(c => `
          <article class="admin-card-item">
            <div class="admin-card-check"><input class="category-check" type="checkbox" data-id="${escapeHTML(c.id)}"></div>
            <div class="admin-card-media">${adminImageMarkup(c.image, c.name)}</div>
            <div class="admin-card-content">
              <div class="card-kicker">CATEGORY #${escapeHTML(c.id)}</div>
              <h3>${escapeHTML(c.name || "-")}</h3>
              <p>${escapeHTML(c.description || "No description.")}</p>
              <div class="card-meta">🎮 ${escapeHTML(c.game_name || findGame(c.game_id)?.name || "-")} · ${c.active ? "🟢 Active" : "🔴 Inactive"}</div>
              <div class="card-actions">
                <button class="btn tiny" type="button" data-category-edit="${escapeHTML(c.id)}">Edit</button>
                <button class="btn tiny" type="button" data-category-toggle="${escapeHTML(c.id)}">${c.active ? "Disable" : "Enable"}</button>
                <button class="btn tiny danger" type="button" data-category-delete="${escapeHTML(c.id)}">Delete</button>
              </div>
            </div>
          </article>
        `).join("")}
      </div>`;
    const update = () => {
        selectedCategoryIds = new Set([...document.querySelectorAll(".category-check:checked")].map(x => Number(x.dataset.id)));
        $("categoriesSelectedCount").textContent = `${selectedCategoryIds.size} selected`;
        $("bulkDeleteCategories").disabled = !selectedCategoryIds.size;
    };
    document.querySelectorAll(".category-check").forEach(x => x.addEventListener("change", update));
    $("selectAllCategories")?.addEventListener("change", e => {
        document.querySelectorAll(".category-check").forEach(x => x.checked = e.target.checked);
        update();
    });
    $("bulkDeleteCategories")?.addEventListener("click", async () => {
        if (!selectedCategoryIds.size || !confirm(`Delete ${selectedCategoryIds.size} categories?`)) return;
        try {
            await apiFetch(`${API}/bulk-delete/categories`, {method:"POST", body:JSON.stringify({ids:[...selectedCategoryIds]})});
            selectedCategoryIds.clear();
            await loadCategories();
            await loadProducts();
        } catch (e) { alert(e.message); }
    });
    document.querySelectorAll("[data-category-edit]").forEach(b => b.addEventListener("click", () => editCategory(Number(b.dataset.categoryEdit))));
    document.querySelectorAll("[data-category-toggle]").forEach(b => b.addEventListener("click", async () => {
        try { await apiFetch(`${API}/categories/${b.dataset.categoryToggle}/toggle`, {method:"POST"}); await loadCategories(); }
        catch(e){ alert(e.message); }
    }));
    document.querySelectorAll("[data-category-delete]").forEach(b => b.addEventListener("click", async () => {
        if (!confirm("Delete this category?")) return;
        try { await apiFetch(`${API}/categories/${b.dataset.categoryDelete}`, {method:"DELETE"}); await loadCategories(); await loadProducts(); }
        catch(e){ alert(e.message); }
    }));
}

function populateCategorySelect() {
    const select = $("productCategory");
    if (!select) return;

    const currentValue = select.value;
    select.innerHTML = `<option value="">Select category</option>`;

    currentCategories.forEach((category) => {
        const option = document.createElement("option");
        option.value = category.id;
        option.textContent =
            category.name || `Category ${category.id}`;
        select.appendChild(option);
    });

    if (currentValue) select.value = currentValue;
}

function populateCategoryGameFilter() {
    const select = $("categoryGameFilter");
    if (!select) return;

    const currentValue = select.value;
    select.innerHTML = `<option value="">All games</option>`;

    currentGames.forEach((game) => {
        const option = document.createElement("option");
        option.value = game.id;
        option.textContent = game.name || `Game ${game.id}`;
        select.appendChild(option);
    });

    select.value = currentValue;
}

/* =========================================================
   PRODUCTS
   ========================================================= */

async function loadProducts() {
    const box = $("productsTable");
    setContainerLoading("productsTable");

    try {
        const data = await apiFetch(`${API}/products`);
        currentProducts = normalizeList(data, ["products", "items"]);

        populateProductGameFilter();
        populateProductCategoryFilter();
        populateInventoryProductSelect();
        renderProducts();
        renderLowStock();
    } catch (error) {
        console.error("[ADMIN] Products error:", error);
        if (box) {
            box.innerHTML = `
                <div class="empty error">
                    ${escapeHTML(error.message)}
                </div>
            `;
        }
    }
}

function renderProducts() {
    const box = $("productsTable"); if (!box) return;
    const search = $("productSearch")?.value?.trim().toLowerCase() || ""; const gameFilter = $("productGameFilter")?.value || ""; const categoryFilter = $("productCategoryFilter")?.value || ""; const statusFilter = $("productStatusFilter")?.value || "";
    let products=[...currentProducts];
    if(search) products=products.filter(p=>[p.id,p.name,p.game_name,p.category_name,p.description].join(" ").toLowerCase().includes(search));
    if(gameFilter) products=products.filter(p=>String(p.game_id||"")===String(gameFilter));
    if(categoryFilter) products=products.filter(p=>String(p.category_id||"")===String(categoryFilter));
    if(statusFilter==="active")products=products.filter(productIsActive); else if(statusFilter==="inactive")products=products.filter(p=>!productIsActive(p));
    if(!products.length){box.innerHTML=`<div class="empty">No products found</div>`;return;}
    box.innerHTML=`<div class="bulk-toolbar"><label><input type="checkbox" id="selectAllProducts"> Select all</label><span id="productsSelectedCount">0 selected</span><button class="btn tiny danger" id="bulkDeleteProducts" disabled>Delete selected</button></div><div class="admin-card-grid">${products.map(p=>{const active=productIsActive(p);const disc=Number(p.discount_percent||0);const image=p.first_image||p.image||p.banner||"";return `<article class="admin-card-item product-admin-card"><div class="admin-card-check"><input class="product-check" type="checkbox" data-id="${escapeHTML(p.id)}"></div><div class="admin-card-media">${adminImageMarkup(image,p.name)}</div><div class="card-badges"><span>${active?"Active":"Inactive"}</span>${disc?`<span>${disc}% OFF</span>`:""}</div><h3>${escapeHTML(p.name||"-")}</h3><p>${escapeHTML(p.description||"")}</p><small>${escapeHTML(p.game_name||"-")} · ${escapeHTML(p.category_name||"-")}</small><strong>⭐ ${Number(p.price_stars||0)} · Stock ${Number(p.stock||0)}</strong><div class="actions"><button class="btn tiny" onclick="viewProductDetails(${Number(p.id)})">View</button><button class="btn tiny" onclick="editProduct(${Number(p.id)})">Edit</button><button class="btn tiny ${active?"danger":"success"}" onclick="toggleProduct(${Number(p.id)},${active})">${active?"Deactivate":"Activate"}</button><button class="btn tiny danger" onclick="deleteProduct(${Number(p.id)})">Delete</button></div></article>`}).join("")}</div>`;
    const update=()=>{selectedProductIds=new Set([...document.querySelectorAll(".product-check:checked")].map(x=>Number(x.dataset.id))); $("productsSelectedCount").textContent=`${selectedProductIds.size} selected`; $("bulkDeleteProducts").disabled=!selectedProductIds.size;};
    document.querySelectorAll(".product-check").forEach(x=>x.addEventListener("change",update)); $("selectAllProducts")?.addEventListener("change",e=>{document.querySelectorAll(".product-check").forEach(x=>x.checked=e.target.checked);update();});
    $("bulkDeleteProducts")?.addEventListener("click",async()=>{if(!confirm(`Delete ${selectedProductIds.size} selected products? Only inactive products with no available inventory can be permanently deleted.`))return;try{const result=await apiFetch(`${API}/bulk-delete/products`,{method:"POST",body:JSON.stringify({ids:[...selectedProductIds]})});selectedProductIds.clear();await loadProducts();const blocked=Object.keys(result?.blocked||{}).length;showMessage(`${Number(result?.deleted_count||0)} product(s) deleted.${blocked?` ${blocked} product(s) were skipped because they are active or still have available inventory.`:""}` , blocked?"error":"success");}catch(e){alert(e.message);}});
}

function populateProductGameFilter() {
    const select = $("productGameFilter");
    if (!select) return;

    const currentValue = select.value;
    select.innerHTML = `<option value="">All games</option>`;

    currentGames.forEach((game) => {
        const option = document.createElement("option");
        option.value = game.id;
        option.textContent = game.name || `Game ${game.id}`;
        select.appendChild(option);
    });

    select.value = currentValue;
}

function populateProductCategoryFilter() {
    const select = $("productCategoryFilter");
    if (!select) return;

    const currentValue = select.value;
    select.innerHTML = `<option value="">All categories</option>`;

    currentCategories.forEach((category) => {
        const option = document.createElement("option");
        option.value = category.id;
        option.textContent =
            category.name || `Category ${category.id}`;
        select.appendChild(option);
    });

    select.value = currentValue;
}


function renderLowStock() {
    const box = $("lowStock");
    if (!box) return;
    const products = [...currentProducts]
        .filter(product => Number(product.stock ?? 0) <= 3)
        .sort((a, b) => Number(a.stock ?? 0) - Number(b.stock ?? 0));
    if (!products.length) {
        box.innerHTML = '<div class="empty">No low-stock products.</div>';
        return;
    }
    box.innerHTML = products.map(product => `
        <div class="low-stock-item">
            <div><strong>${escapeHTML(product.name || "Product")}</strong><small>Stock: ${escapeHTML(product.stock ?? 0)}</small></div>
            <span>${escapeHTML(product.stock ?? 0)}</span>
        </div>
    `).join("");
}

function populateInventoryProductSelect() {
    const modalSelect = $("inventoryProduct");
    const filterSelect = $("inventoryProductFilter");

    if (modalSelect) {
        const current = modalSelect.value;
        modalSelect.innerHTML = '<option value="">Select product</option>';
        currentProducts.forEach(product => {
            const option = document.createElement("option");
            option.value = product.id;
            option.textContent = product.name || `Product ${product.id}`;
            modalSelect.appendChild(option);
        });
        if (current) modalSelect.value = current;
    }

    if (filterSelect) {
        const current = filterSelect.value;
        filterSelect.innerHTML = '<option value="">All products</option>';
        currentProducts.forEach(product => {
            const option = document.createElement("option");
            option.value = product.id;
            option.textContent = product.name || `Product ${product.id}`;
            filterSelect.appendChild(option);
        });
        if (current) filterSelect.value = current;
    }
}

function resetProductForm() {
    resetProductInventoryStep();
    const form = $("productForm");
    if (form) form.reset();

    if ($("productId")) $("productId").value = "";
    if ($("productModalTitle")) $("productModalTitle").textContent = "Add Product";
    if ($("productSubmitText")) $("productSubmitText").textContent = "Create Product";
    if ($("productFeatured")) $("productFeatured").checked = false;
    if ($("productActive")) $("productActive").checked = true;
    if ($("productDiscount")) $("productDiscount").value = "0";
    if ($("productStock")) $("productStock").value = "0";
    if ($("productImages")) $("productImages").value = "";
    if ($("productMessage")) $("productMessage").textContent = "";
    if ($("productUploadMessage")) $("productUploadMessage").textContent = "You can paste image URLs or upload images from your phone/computer. Uploaded images are stored through Telegram and linked to this product.";
    if ($("productImageFiles")) $("productImageFiles").value = "";
}

function openProductModal() {
    resetProductForm();
    populateGameSelects();
    populateCategorySelect();
    openModal("productModal");
}

async function uploadProductImages(event) {
    const input = event?.target;
    const files = Array.from(input?.files || []);
    if (!files.length) return;
    const message = $("productUploadMessage");
    const textarea = $("productImages");
    const banner = $("productBanner");
    if (files.length > 100) {
        if (message) message.textContent = "You can upload up to 100 images at a time.";
        input.value = "";
        return;
    }
    const formData = new FormData();
    files.forEach(file => formData.append("files", file, file.name));
    try {
        if (message) message.textContent = `Uploading ${files.length} image(s)…`;
        const response = await fetch(`${API}/media/upload`, {
            method: "POST",
            credentials: "include",
            cache: "no-store",
            body: formData
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) {
            throw new Error(data.error || (data.errors || []).join("; ") || "Image upload failed.");
        }
        const refs = normalizeList(data.images).map(x => x.reference || x.url || "").filter(Boolean);
        const current = normalizeImageUrls(textarea?.value || "");
        const merged = [...new Set([...current, ...refs])].slice(0, 100);
        if (textarea) textarea.value = merged.join("\n");
        if (banner && !banner.value && refs[0]) banner.value = refs[0];
        const failed = normalizeList(data.errors);
        if (message) message.textContent = `Uploaded ${refs.length} image(s).${failed.length ? ` ${failed.length} failed.` : ""}`;
    } catch (error) {
        console.error("[ADMIN] Image upload error:", error);
        if (message) message.textContent = error.message || "Image upload failed.";
    } finally {
        input.value = "";
    }
}

async function editProduct(productId) {
    try {
        const detail = await apiFetch(`${API}/products/${Number(productId)}`);
        const product = detail?.product;
        if (!product) throw new Error("Product not found.");

        populateGameSelects();
        if ($("productGame")) $("productGame").value = product.game_id ?? "";
        populateCategorySelect();
        if ($("productCategory")) $("productCategory").value = product.category_id ?? "";

        if ($("productId")) $("productId").value = product.id;
        if ($("productName")) $("productName").value = product.name || "";
        if ($("productDescription")) $("productDescription").value = product.description || "";
        if ($("productPrice")) $("productPrice").value = product.price_stars ?? "";
        if ($("productStock")) $("productStock").value = product.stock ?? 0;
        if ($("productDiscount")) $("productDiscount").value = product.discount_percent ?? 0;
        if ($("productFeatured")) $("productFeatured").checked = productIsFeatured(product);
        if ($("productActive")) $("productActive").checked = productIsActive(product);
        if ($("productBanner")) $("productBanner").value = product.banner || "";

        if ($("productImages")) {
            $("productImages").value =
                normalizeList(product.images, ["items"])
                    .map((x) => x.image || "")
                    .filter(Boolean)
                    .join("\n");
        }
        if ($("productImageFiles")) $("productImageFiles").value = "";
        resetProductInventoryStep();
        if ($("productUploadMessage")) $("productUploadMessage").textContent = "You can paste image URLs or upload images from your phone/computer. Uploaded images are stored through Telegram and linked to this product.";

        if ($("productModalTitle")) {
            $("productModalTitle").textContent = "Edit Product";
        }

        if ($("productSubmitText")) {
            $("productSubmitText").textContent = "Save Changes";
        }

        openModal("productModal");
    } catch (error) {
        console.error("[ADMIN] Edit product error:", error);
        showMessage(error.message, "error");
    }
}

function resetProductInventoryStep() {
    const step = $("productInventoryStep");
    const base = $("productBaseFields");
    const actions = $("productBasicActions");
    if (step) step.hidden = true;
    if (base) base.style.display = "";
    if (actions) actions.style.display = "";
    if ($("productInventoryProductName")) $("productInventoryProductName").textContent = "New product";
    if ($("productInventoryProductId")) $("productInventoryProductId").textContent = "Product #—";
    if ($("productInventoryStock")) $("productInventoryStock").textContent = "0 available";
    if ($("productInventoryData")) $("productInventoryData").value = "";
    if ($("productInventoryInputCount")) $("productInventoryInputCount").textContent = "0 items";
    if ($("productInventoryStatus")) $("productInventoryStatus").textContent = "No inventory added yet.";
    if ($("productInventoryMessage")) $("productInventoryMessage").textContent = "";
}

function showProductInventoryStep(productId, productName, availableStock = 0) {
    const step = $("productInventoryStep");
    const base = $("productBaseFields");
    const actions = $("productBasicActions");
    if (!step) return;
    if (base) base.style.display = "none";
    if (actions) actions.style.display = "none";
    step.hidden = false;
    if ($("productInventoryProductName")) $("productInventoryProductName").textContent = productName || "New product";
    if ($("productInventoryProductId")) $("productInventoryProductId").textContent = `Product #${Number(productId)}`;
    if ($("productInventoryStock")) $("productInventoryStock").textContent = `${Number(availableStock || 0)} available`;
    if ($("productInventoryData")) $("productInventoryData").focus();
}

function parseInventoryRecordCount(text) {
    const value = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
    if (!value) return 0;
    const markers = value.match(/^\s*\d+\s*a\s*:/gim);
    if (markers && markers.length) return markers.length;
    return value.split("\n").map(v => v.trim()).filter(Boolean).length;
}

function parseProductInventoryTextarea() {
    return $("productInventoryData")?.value || "";
}

function updateProductInventoryInputCount() {
    const count = $("productInventoryInputCount");
    if (!count) return;
    const rawText = parseProductInventoryTextarea();
    const items = parseInventoryRecordCount(rawText);
    count.textContent = `${items} item${items === 1 ? "" : "s"}`;
}

let inventoryPreviewState = null;

function ensureInventoryPreviewModal() {
    let modal = $("inventoryPreviewModal");
    if (modal) return modal;

    modal = document.createElement("div");
    modal.id = "inventoryPreviewModal";
    modal.className = "modal";
    modal.innerHTML = `
        <div class="modal-card wide inventory-preview-card">
            <div class="modal-head">
                <div>
                    <h2>Inventory Import Preview</h2>
                    <p id="inventoryPreviewProduct" class="form-hint">Review before anything is imported.</p>
                </div>
                <button type="button" class="icon-btn" id="inventoryPreviewCloseBtn">×</button>
            </div>
            <div id="inventoryPreviewSummary" class="inventory-preview-summary"></div>
            <div id="inventoryPreviewNotice" class="form-message"></div>
            <div id="inventoryPreviewEditor" class="inventory-preview-editor"></div>
            <div class="modal-actions inventory-preview-actions">
                <button type="button" class="btn ghost" id="inventoryPreviewCancelBtn">Cancel</button>
                <button type="button" class="btn ghost" id="inventoryPreviewRecheckBtn">Recheck</button>
                <button type="button" class="btn primary" id="inventoryPreviewConfirmBtn" disabled>Confirm Import</button>
            </div>
        </div>`;
    document.body.appendChild(modal);

    modal.addEventListener("click", (event) => {
        if (event.target === modal) closeInventoryPreview();
    });
    $("inventoryPreviewCloseBtn")?.addEventListener("click", closeInventoryPreview);
    $("inventoryPreviewCancelBtn")?.addEventListener("click", closeInventoryPreview);
    $("inventoryPreviewRecheckBtn")?.addEventListener("click", recheckInventoryPreview);
    $("inventoryPreviewConfirmBtn")?.addEventListener("click", confirmInventoryPreview);
    return modal;
}

function closeInventoryPreview() {
    const modal = $("inventoryPreviewModal");
    if (modal) {
        modal.classList.remove("active");
        modal.style.display = "none";
    }
    inventoryPreviewState = null;
}

function openInventoryPreview(preview, context) {
    inventoryPreviewState = {
        context,
        preview,
        mode: preview.mode || "legacy",
        parserInput: context.rawText || "",
    };
    const modal = ensureInventoryPreviewModal();
    renderInventoryPreview(preview);
    modal.classList.add("active");
    modal.style.display = "flex";
}

function inventoryStatusLabel(status) {
    const labels = {
        ready: "READY",
        existing: "ALREADY EXISTS",
        duplicate: "DUPLICATE",
        invalid: "INVALID"
    };
    return labels[status] || String(status || "UNKNOWN").toUpperCase();
}

function renderInventoryPreview(preview) {
    ensureInventoryPreviewModal();
    const summary = $("inventoryPreviewSummary");
    const editor = $("inventoryPreviewEditor");
    const notice = $("inventoryPreviewNotice");
    const product = $("inventoryPreviewProduct");
    const confirmButton = $("inventoryPreviewConfirmBtn");

    if (product) {
        product.textContent = `${preview.product_name || "Product"} • Product #${Number(preview.product_id || 0)} • Nothing has been imported yet`;
    }

    if (summary) {
        const items = [
            ["Total", preview.total_count],
            ["Valid", preview.valid_count],
            ["Invalid", preview.invalid_count],
            ["Duplicates", preview.duplicate_count],
            ["Already exists", preview.existing_count],
            ["Ready to import", preview.ready_count],
        ];
        summary.innerHTML = items.map(([label, value]) => `
            <div class="inventory-preview-stat">
                <span>${escapeHTML(label)}</span>
                <strong>${Number(value || 0)}</strong>
            </div>`).join("");
    }

    if (notice) {
        notice.className = "form-message";
        if (preview.parser_error) {
            notice.className = "form-message error";
            notice.textContent = "The pasted account markers are invalid. Edit the original input below and click Recheck.";
        } else if (Number(preview.invalid_count || 0) > 0) {
            notice.className = "form-message error";
            notice.textContent = "Fix the invalid records below, then click Recheck. Nothing is written to the database during preview or recheck.";
        } else if (Number(preview.ready_count || 0) > 0) {
            notice.className = "form-message success";
            notice.textContent = "Review the records carefully. Confirm Import is the only action that writes inventory.";
        } else {
            notice.className = "form-message error";
            notice.textContent = "There are no new inventory items ready to import.";
        }
    }

    if (editor) editor.innerHTML = "";

    if (preview.parser_error) {
        const label = document.createElement("label");
        label.className = "inventory-preview-source-label";
        label.textContent = "Original input";
        const textarea = document.createElement("textarea");
        textarea.id = "inventoryPreviewSourceText";
        textarea.className = "inventory-preview-source";
        textarea.rows = 12;
        textarea.value = inventoryPreviewState?.parserInput || "";
        label.appendChild(textarea);
        editor?.appendChild(label);
    } else {
        const records = Array.isArray(preview.records) ? preview.records : [];
        records.forEach((record, position) => {
            const wrapper = document.createElement("div");
            wrapper.className = "inventory-preview-record";

            const head = document.createElement("div");
            head.className = "inventory-preview-record-head";

            const title = document.createElement("strong");
            title.textContent = `#${Number(record.index || position + 1)}${record.marker ? ` • ${record.marker}` : ""}`;
            const badge = document.createElement("span");
            badge.className = `inventory-preview-badge ${String(record.status || "").toLowerCase()}`;
            badge.textContent = inventoryStatusLabel(record.status);
            head.appendChild(title);
            head.appendChild(badge);
            wrapper.appendChild(head);

            const textarea = document.createElement("textarea");
            textarea.className = "inventory-preview-item";
            textarea.rows = Math.min(8, Math.max(3, String(record.item_data || "").split("\n").length + 1));
            textarea.value = String(record.item_data || "");
            textarea.dataset.previewIndex = String(record.index || position + 1);
            wrapper.appendChild(textarea);

            const reason = document.createElement("div");
            reason.className = "inventory-preview-reason";
            reason.textContent = String(record.reason || "");
            wrapper.appendChild(reason);

            editor?.appendChild(wrapper);
        });
    }

    if (confirmButton) {
        confirmButton.disabled = preview.parser_error || Number(preview.invalid_count || 0) > 0 || Number(preview.ready_count || 0) <= 0;
        confirmButton.textContent = "Confirm Import";
    }
}

async function requestInventoryPreview(productId, input) {
    const data = {
        product_id: Number(productId),
        mode: input.mode || "legacy"
    };
    if (input.bulkText !== undefined) data.bulk_text = input.bulkText;
    if (input.items !== undefined) data.items = input.items;

    return apiFetch(`${API}/inventory/preview`, {
        method: "POST",
        body: JSON.stringify(data)
    });
}

async function showInventoryPreviewForRawText(productId, rawText, contextType) {
    const text = String(rawText || "");
    const count = parseInventoryRecordCount(text);
    if (!count) throw new Error("Enter at least one inventory item.");
    if (count > 500) throw new Error("Maximum 500 inventory items per preview.");

    const preview = await requestInventoryPreview(productId, {
        bulkText: text,
        mode: /(^|\n)\s*\d+\s*a\s*:/i.test(text) ? "marker" : "legacy"
    });
    openInventoryPreview(preview, {
        type: contextType,
        productId: Number(productId),
        rawText: text
    });
    return preview;
}

async function addInventoryFromProductStep() {
    const productId = Number($("productId")?.value || 0);
    const productName = $("productInventoryProductName")?.textContent || "Product";
    const rawText = parseProductInventoryTextarea();
    if (!productId) return showMessage("Create the product first.", "error");
    const button = $("productInventoryAddBtn");
    const message = $("productInventoryMessage");
    if (button) { button.disabled = true; button.textContent = "Preparing preview…"; }
    if (message) { message.className = "form-message"; message.textContent = "Checking inventory without importing…"; }

    try {
        await showInventoryPreviewForRawText(productId, rawText, "product-step");
        if (message) message.textContent = "Preview opened. Nothing has been imported yet.";
    } catch (error) {
        console.error("[ADMIN] Product-step inventory preview error:", error);
        if (message) { message.className = "form-message error"; message.textContent = error.message; }
    } finally {
        if (button) { button.disabled = false; button.textContent = "Preview Inventory"; }
    }
}

async function saveInventory(event) {
    event.preventDefault();
    const productId = Number($("inventoryProduct")?.value || 0);
    const rawText = parseInventoryTextarea();
    if (!productId) return showMessage("Please select a product.", "error");
    const button = document.querySelector('#inventoryForm button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = "Preparing preview…"; }
    try {
        await showInventoryPreviewForRawText(productId, rawText, "inventory-modal");
    } catch (error) {
        console.error("[ADMIN] Inventory preview error:", error);
        showMessage(error.message, "error");
    } finally {
        if (button) { button.disabled = false; button.textContent = "Preview Inventory"; }
    }
}

async function recheckInventoryPreview() {
    const state = inventoryPreviewState;
    if (!state) return;

    const button = $("inventoryPreviewRecheckBtn");
    const confirmButton = $("inventoryPreviewConfirmBtn");
    if (button) { button.disabled = true; button.textContent = "Checking…"; }
    if (confirmButton) confirmButton.disabled = true;

    try {
        let preview;
        if (state.preview.parser_error) {
            const source = $("inventoryPreviewSourceText")?.value || "";
            state.parserInput = source;
            const count = parseInventoryRecordCount(source);
            if (!count) throw new Error("Enter at least one inventory item.");
            if (count > 500) throw new Error("Maximum 500 inventory items per preview.");
            preview = await requestInventoryPreview(state.preview.product_id, {
                bulkText: source,
                mode: /(^|\n)\s*\d+\s*a\s*:/i.test(source) ? "marker" : "legacy"
            });
        } else {
            const textareas = [...document.querySelectorAll("#inventoryPreviewEditor .inventory-preview-item")];
            const items = textareas.map((textarea) => textarea.value);
            if (!items.length) throw new Error("There are no inventory records to recheck.");
            preview = await requestInventoryPreview(state.preview.product_id, {
                items,
                mode: state.mode
            });
        }
        state.preview = preview;
        state.mode = preview.mode || state.mode;
        renderInventoryPreview(preview);
    } catch (error) {
        console.error("[ADMIN] Inventory recheck error:", error);
        const notice = $("inventoryPreviewNotice");
        if (notice) { notice.className = "form-message error"; notice.textContent = error.message; }
    } finally {
        if (button) { button.disabled = false; button.textContent = "Recheck"; }
    }
}

async function confirmInventoryPreview() {
    const state = inventoryPreviewState;
    if (!state || !state.preview || state.preview.parser_error) return;
    const preview = state.preview;
    if (Number(preview.invalid_count || 0) > 0 || Number(preview.ready_count || 0) <= 0) return;

    const confirmButton = $("inventoryPreviewConfirmBtn");
    const cancelButton = $("inventoryPreviewCancelBtn");
    if (confirmButton) { confirmButton.disabled = true; confirmButton.textContent = "Importing…"; }
    if (cancelButton) cancelButton.disabled = true;

    try {
        const data = await apiFetch(`${API}/inventory`, {
            method: "POST",
            body: JSON.stringify({
                product_id: Number(preview.product_id),
                items: Array.isArray(preview.ready_items) ? preview.ready_items : []
            })
        });

        const inserted = Number(data.inserted || 0);
        const skipped = Number(data.duplicate_count || data.skipped_existing || 0);
        const stock = Number(data.available_stock || 0);
        const context = state.context || {};

        if (context.type === "product-step") {
            if ($("productInventoryStock")) $("productInventoryStock").textContent = `${stock} available`;
            if ($("productInventoryStatus")) $("productInventoryStatus").innerHTML = `<strong>${stock}</strong> inventory item(s) available for ${escapeHTML($("productInventoryProductName")?.textContent || "Product")}.`;
            if ($("productInventoryData")) $("productInventoryData").value = "";
            updateProductInventoryInputCount();
            if ($("productInventoryMessage")) {
                $("productInventoryMessage").className = "form-message success";
                $("productInventoryMessage").textContent = `${inserted} inventory item(s) imported.${skipped ? ` ${skipped} duplicate(s) skipped.` : ""}`;
            }
        } else {
            closeModal("inventoryModal");
        }

        closeInventoryPreview();
        await Promise.all([loadProducts(), loadInventory(), loadDashboard()]);
    } catch (error) {
        console.error("[ADMIN] Confirm inventory import error:", error);
        const notice = $("inventoryPreviewNotice");
        if (notice) { notice.className = "form-message error"; notice.textContent = error.message; }
        if (confirmButton) { confirmButton.disabled = false; confirmButton.textContent = "Confirm Import"; }
        if (cancelButton) cancelButton.disabled = false;
    }
}



async function saveProduct(event) {
    event?.preventDefault();
    const id = $("productId")?.value?.trim() || "";
    const name = $("productName")?.value?.trim() || "";
    const description = $("productDescription")?.value?.trim() || "";
    const gameId = $("productGame")?.value || "";
    const categoryId = $("productCategory")?.value || "";
    const price = Number($("productPrice")?.value);
    const stock = Number($("productStock")?.value);
    const discount = Number($("productDiscount")?.value || 0);
    const featured = $("productFeatured")?.checked === true;
    const active = $("productActive")?.checked === true;
    const banner = $("productBanner")?.value?.trim() || "";
    const images = normalizeImageUrls($("productImages")?.value || "");

    if (!name) return showMessage("Product name is required.", "error");
    if (!gameId) return showMessage("Please select a game.", "error");
    if (!categoryId) return showMessage("Please select a category.", "error");
    if (!Number.isFinite(price) || price <= 0) return showMessage("Stars price must be greater than 0.", "error");
    if (!Number.isFinite(stock) || stock < 0) return showMessage("Stock cannot be negative.", "error");
    if (!Number.isFinite(discount) || discount < 0 || discount > 100) return showMessage("Discount must be between 0 and 100.", "error");

    const payload = {
        name,
        description,
        game_id: Number(gameId),
        category_id: Number(categoryId),
        price_stars: price,
        stock,
        featured,
        active,
        discount_percent: discount,
        banner,
        images
    };

    const submit = $("productSubmitText");
    if (submit) { submit.disabled = true; submit.textContent = id ? "Saving…" : "Creating…"; }
    try {
        await apiFetch(id ? `${API}/products/${Number(id)}` : `${API}/products`, {
            method: id ? "PUT" : "POST",
            body: JSON.stringify(payload)
        });
        closeModal("productModal");
        showMessage(id ? "Product updated successfully." : "Product created successfully.");
        await Promise.all([loadProducts(), loadDashboard()]);
    } catch (error) {
        console.error("[ADMIN] Save product error:", error);
        showMessage(error.message || "Unable to save product.", "error");
    } finally {
        if (submit) { submit.disabled = false; submit.textContent = id ? "Save Changes" : "Create Product"; }
    }
}

async function viewProductDetails(productId) {
    try {
        const data = await apiFetch(`${API}/products/${Number(productId)}`);
        const product = data?.product;
        if (!product) throw new Error("Product not found.");

        const box = $("productDetailsContent");
        if (!box) throw new Error("Product details container is missing.");

        const images = normalizeList(product.images, ["items"])
            .map((img) => adminImageSrc(img?.image || img?.url || ""))
            .filter(Boolean);
        const deps = product.dependencies || {};
        const active = productIsActive(product);
        const featured = productIsFeatured(product);
        const stock = Number(product.available_inventory ?? product.stock ?? 0);

        box.innerHTML = `
            <div class="detail-grid">
                <div><span class="muted">Product ID</span><strong>#${escapeHTML(product.id)}</strong></div>
                <div><span class="muted">Status</span><strong>${active ? "Active" : "Inactive"}</strong></div>
                <div><span class="muted">Name</span><strong>${escapeHTML(product.name || "-")}</strong></div>
                <div><span class="muted">Price</span><strong>⭐ ${escapeHTML(product.price_stars ?? 0)}</strong></div>
                <div><span class="muted">Game</span><strong>${escapeHTML(product.game_name || findGame(product.game_id)?.name || "-")}</strong></div>
                <div><span class="muted">Category</span><strong>${escapeHTML(product.category_name || findCategory(product.category_id)?.name || "-")}</strong></div>
                <div><span class="muted">Available stock</span><strong>${stock}</strong></div>
                <div><span class="muted">Discount</span><strong>${escapeHTML(product.discount_percent ?? 0)}%</strong></div>
                <div><span class="muted">Featured</span><strong>${featured ? "Yes" : "No"}</strong></div>
                <div><span class="muted">Created</span><strong>${escapeHTML(formatDate(product.created_at))}</strong></div>
            </div>
            <div class="detail-section">
                <span class="muted">Description</span>
                <p>${escapeHTML(product.description || "No description.")}</p>
            </div>
            <div class="detail-section">
                <span class="muted">Dependencies</span>
                <div class="dependency-grid">
                    <span>Orders: <strong>${Number(deps.orders || 0)}</strong></span>
                    <span>Inventory: <strong>${Number(deps.inventory || 0)}</strong></span>
                    <span>Cart items: <strong>${Number(deps.cart_items || 0)}</strong></span>
                    <span>Favorites: <strong>${Number(deps.favorites || 0)}</strong></span>
                    <span>Reviews: <strong>${Number(deps.reviews || 0)}</strong></span>
                </div>
            </div>
            <div class="detail-section">
                <span class="muted">Images (${images.length})</span>
                <div class="product-detail-images">
                    ${images.length ? images.map((src, i) => `<a href="${escapeHTML(src)}" target="_blank" rel="noopener"><img src="${escapeHTML(src)}" alt="Product image ${i + 1}" loading="lazy"></a>`).join("") : '<div class="empty">No product images.</div>'}
                </div>
            </div>
        `;

        $("productDetailsEditBtn")?.addEventListener("click", async () => {
            closeModal("productDetailsModal");
            await editProduct(product.id);
        }, { once: true });

        $("productDetailsSyncBtn")?.addEventListener("click", async () => {
            const btn = $("productDetailsSyncBtn");
            if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }
            try {
                const sync = await apiFetch(`${API}/products/${Number(product.id)}/sync-stock`, { method: "POST" });
                const fresh = await apiFetch(`${API}/products/${Number(product.id)}`);
                await loadProducts();
                await loadDashboard();
                await viewProductDetails(product.id);
                showMessage(sync?.message || `Stock synchronized: ${Number(fresh?.product?.available_inventory ?? fresh?.product?.stock ?? 0)} available.`);
            } catch (error) {
                showMessage(error.message || "Unable to sync stock.", "error");
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = "Sync Stock"; }
            }
        }, { once: true });

        openModal("productDetailsModal");
    } catch (error) {
        console.error("[ADMIN] Product details error:", error);
        showMessage(error.message || "Unable to load product details.", "error");
    }
}

async function deleteProduct(productId) {
    const product = findProduct(productId);
    const name = product?.name || `Product #${Number(productId)}`;
    if (!window.confirm(`Permanently delete ${name}? Only inactive products with no available inventory can be deleted.`)) return;

    try {
        const data = await apiFetch(`${API}/products/${Number(productId)}`, { method: "DELETE" });
        showMessage(data?.message || "Product deleted successfully.");
        await Promise.all([loadProducts(), loadDashboard()]);
    } catch (error) {
        showMessage(error.message || "Unable to delete product.", "error");
    }
}

async function toggleProduct(productId, currentActive) {
    const action = currentActive ? "Deactivate" : "Activate";
    if (!window.confirm(`${action} this product?`)) return;

    try {
        await apiFetch(`${API}/products/${Number(productId)}/toggle`, { method: "POST" });
        showMessage(currentActive ? "Product deactivated successfully." : "Product activated successfully.");
        await Promise.all([loadProducts(), loadDashboard()]);
    } catch (error) {
        showMessage(error.message || "Unable to update product.", "error");
    }
}

async function loadInventory() {
    const box = $("inventoryTable");
    setContainerLoading("inventoryTable");
    try {
        const data = await apiFetch(`${API}/inventory`);
        currentInventory = normalizeList(data, ["inventory", "items"]);
        const productFilter = $("inventoryProductFilter")?.value || "";
        const statusFilter = $("inventoryStatusFilter")?.value || "";
        const search = $("inventorySearch")?.value?.trim().toLowerCase() || "";

        let items = [...currentInventory];
        if (productFilter) items = items.filter(x => String(x.product_id ?? "") === String(productFilter));
        if (statusFilter) items = items.filter(x => String(x.status || "") === String(statusFilter));
        if (search) items = items.filter(x => [x.id, x.product_id, x.product_name, x.item_data, x.status].join(" ").toLowerCase().includes(search));

        const total = currentInventory.length;
        const available = currentInventory.filter(x => String(x.status || "").toLowerCase() === "available").length;
        const sold = currentInventory.filter(x => String(x.status || "").toLowerCase() === "sold").length;
        $("inventoryTotalCount") && ($("inventoryTotalCount").textContent = String(total));
        $("inventoryAvailableCount") && ($("inventoryAvailableCount").textContent = String(available));
        $("inventorySoldCount") && ($("inventorySoldCount").textContent = String(sold));

        populateInventoryProductSelect();
        const visibleIds = new Set(items.map(x => Number(x.id)));
        selectedInventoryIds = new Set([...selectedInventoryIds].filter(id => visibleIds.has(Number(id))));
        const count = selectedInventoryIds.size;
        $("inventorySelectedCount") && ($("inventorySelectedCount").textContent = `${count} selected`);
        $("inventoryBulkDeleteBtn") && ($("inventoryBulkDeleteBtn").disabled = count === 0);
        if ($("inventorySelectAll")) $("inventorySelectAll").checked = items.length > 0 && items.every(x => selectedInventoryIds.has(Number(x.id)));

        if (!items.length) {
            if (box) box.innerHTML = '<div class="empty">No inventory available</div>';
            return;
        }

        box.innerHTML = `
            <table><thead><tr>
                <th></th><th>ID</th><th>Product</th><th>Status</th><th>Item Data</th><th>Created</th><th>Sold</th><th>Actions</th>
            </tr></thead><tbody>
            ${items.map(item => {
                const id = Number(item.id);
                const status = String(item.status || "available").toLowerCase();
                const selected = selectedInventoryIds.has(id);
                const productName = item.product_name || findProduct(item.product_id)?.name || `Product #${item.product_id ?? "-"}`;
                return `<tr>
                    <td><input type="checkbox" class="inventory-row-check" data-id="${id}" ${selected ? "checked" : ""}></td>
                    <td>#${escapeHTML(id)}</td>
                    <td>${escapeHTML(productName)}</td>
                    <td>${escapeHTML(status)}</td>
                    <td><code class="inventory-data-cell">${escapeHTML(item.item_data || "-")}</code></td>
                    <td>${escapeHTML(formatDate(item.created_at))}</td>
                    <td>${escapeHTML(formatDate(item.sold_at))}</td>
                    <td>${status === "available" ? `<button class="btn tiny danger" data-inventory-sold="${id}">Mark sold</button>` : "<span class=\"muted\">History</span>"} <button class="btn tiny danger" data-inventory-delete="${id}">Delete</button></td>
                </tr>`;
            }).join("")}
            </tbody></table>`;

        box.querySelectorAll(".inventory-row-check").forEach(el => el.addEventListener("change", () => toggleInventorySelection(Number(el.dataset.id), el.checked)));
        box.querySelectorAll("[data-inventory-sold]").forEach(btn => btn.addEventListener("click", () => changeInventoryStatus(Number(btn.dataset.inventorySold), "sold")));
        box.querySelectorAll("[data-inventory-delete]").forEach(btn => btn.addEventListener("click", () => deleteInventoryItem(Number(btn.dataset.inventoryDelete))));
    } catch (error) {
        console.error("[ADMIN] Inventory error:", error);
        if (box) box.innerHTML = `<div class="empty error">${escapeHTML(error.message || "Unable to load inventory.")}</div>`;
    }
}

function toggleInventorySelection(inventoryId, checked) {
    const id = Number(inventoryId);
    if (!Number.isFinite(id)) return;
    if (checked) selectedInventoryIds.add(id);
    else selectedInventoryIds.delete(id);
    const count = selectedInventoryIds.size;
    $("inventorySelectedCount") && ($("inventorySelectedCount").textContent = `${count} selected`);
    $("inventoryBulkDeleteBtn") && ($("inventoryBulkDeleteBtn").disabled = count === 0);
    const visible = [...document.querySelectorAll(".inventory-row-check")];
    if ($("inventorySelectAll")) $("inventorySelectAll").checked = visible.length > 0 && visible.every(x => x.checked);
}

function toggleAllInventory(checked) {
    document.querySelectorAll(".inventory-row-check").forEach(el => {
        el.checked = !!checked;
        const id = Number(el.dataset.id);
        if (checked) selectedInventoryIds.add(id);
        else selectedInventoryIds.delete(id);
    });
    const count = selectedInventoryIds.size;
    $("inventorySelectedCount") && ($("inventorySelectedCount").textContent = `${count} selected`);
    $("inventoryBulkDeleteBtn") && ($("inventoryBulkDeleteBtn").disabled = count === 0);
}

function updateInventoryInputCount() {
    const el = $("inventoryInputCount");
    if (!el) return;
    const raw = $("inventoryItemData")?.value || "";
    const count = parseInventoryRecordCount(raw);
    el.textContent = `${count} item${count === 1 ? "" : "s"}`;
}

function openInventoryModal() {
    const form = $("inventoryForm");
    if (form) form.reset();
    updateInventoryInputCount();
    populateInventoryProductSelect();
    openModal("inventoryModal");
}

function finishProductSetup() {
    closeModal("productModal");
    resetProductInventoryStep();
    Promise.all([loadProducts(), loadInventory(), loadDashboard()]).catch((error) => {
        console.error("[ADMIN] Final product setup refresh failed:", error);
    });
}

async function changeInventoryStatus(inventoryId, newStatus) {
    const item = currentInventory.find(
        (entry) =>
            Number(entry.id) ===
            Number(inventoryId)
    );

    if (!item) {
        showMessage(
            "Inventory item not found.",
            "error"
        );
        return;
    }

    const currentStatus =
        String(
            item.status ||
            "available"
        ).toLowerCase();

    if (currentStatus === newStatus) {
        return;
    }

    if (currentStatus === "sold" || newStatus !== "sold") {
        showMessage(
            "Sold inventory is kept for history and cannot be returned to available. Add a new inventory item instead.",
            "error"
        );
        return;
    }

    const warning = "Mark this inventory item as SOLD? It will be removed from available stock.";

    if (!window.confirm(warning)) {
        return;
    }

    try {
        const data =
            await apiFetch(
                `${API}/inventory/${inventoryId}/status`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        status: newStatus
                    })
                }
            );

        showMessage(
            `Inventory #${inventoryId} is now ${newStatus}. ` +
            `Available stock: ${
                data.available_stock ?? "-"
            }.`
        );

        await Promise.all([
            loadInventory(),
            loadProducts(),
            loadDashboard()
        ]);
    } catch (error) {
        console.error(
            "[ADMIN] Inventory status error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}
async function deleteInventoryItem(inventoryId) {
    const item = currentInventory.find(
        (entry) =>
            Number(entry.id) ===
            Number(inventoryId)
    );

    if (!item) {
        showMessage(
            "Inventory item not found.",
            "error"
        );
        return;
    }

    if (
        !window.confirm(
            "Delete this inventory item? Sold items will be kept for history."
        )
    ) {
        return;
    }

    try {
        const data = await apiFetch(
            `${API}/inventory/${inventoryId}`,
            {
                method: "DELETE"
            }
        );

        selectedInventoryIds.delete(
            Number(inventoryId)
        );

        showMessage(
            data?.message ||
            "Inventory item deleted.",
            "success"
        );

        await loadInventory();
    } catch (error) {
        console.error(
            "[ADMIN] Inventory delete error:",
            error
        );

        showMessage(
            error.message ||
            "Failed to delete inventory item.",
            "error"
        );
    }
}
async function deleteSelectedInventory() {
    const ids =
        Array.from(
            selectedInventoryIds
        );

    if (!ids.length) {
        showMessage(
            "Select at least one available inventory item.",
            "error"
        );
        return;
    }

    if (
        !window.confirm(
            `Delete ${ids.length} selected inventory item(s)? Sold items will be kept for history.`
        )
    ) {
        return;
    }

    try {
        const data =
            await apiFetch(
                `${API}/inventory/bulk-delete`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        ids
                    })
                }
            );

        selectedInventoryIds.clear();

        let message =
            `${Number(
                data.deleted ?? 0
            )} item(s) deleted.`;

        if (
            Number(
                data.skipped_sold ?? 0
            ) > 0
        ) {
            message +=
                ` ${Number(
                    data.skipped_sold
                )} sold item(s) kept for history.`;
        }

        if (
            Number(
                data.not_found ?? 0
            ) > 0
        ) {
            message +=
                ` ${Number(
                    data.not_found
                )} item(s) were not found.`;
        }

        showMessage(message);

        await Promise.all([
            loadInventory(),
            loadProducts(),
            loadDashboard()
        ]);
    } catch (error) {
        console.error(
            "[ADMIN] Bulk delete inventory error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}

/* =========================================================
   ORDERS
   ========================================================= */

async function loadOrders() {
    const box =
        $("ordersTable");

    setContainerLoading(
        "ordersTable"
    );

    try {
        const data =
            await apiFetch(
                `${API}/orders`
            );

        currentOrders =
            normalizeList(
                data,
                [
                    "orders",
                    "items"
                ]
            );

        renderOrders();
    } catch (error) {
        console.error(
            "[ADMIN] Orders error:",
            error
        );

        if (box) {
            box.innerHTML = `
                <div class="empty error">
                    ${escapeHTML(
                        error.message
                    )}
                </div>
            `;
        }
    }
}

function renderOrders() {
    const box =
        $("ordersTable");

    if (!box) return;

    const search =
        $("orderSearch")
            ?.value
            ?.trim()
            .toLowerCase() ||
        "";

    const statusFilter =
        $("orderStatusFilter")
            ?.value ||
        "";

    let orders =
        [...currentOrders];

    if (search) {
        orders =
            orders.filter(
                (order) => {
                    const haystack = [
                        order.id,
                        order.user_id,
                        order.telegram_id,
                        order.username,
                        order.product_name,
                        order.status
                    ]
                        .join(" ")
                        .toLowerCase();

                    return haystack.includes(
                        search
                    );
                }
            );
    }

    if (statusFilter) {
        orders =
            orders.filter(
                (order) =>
                    String(
                        order.status ||
                        ""
                    ) ===
                    statusFilter
            );
    }

    if (!orders.length) {
        box.innerHTML = `
            <div class="empty">
                No orders available
            </div>
        `;
        return;
    }

    box.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>User</th>
                    <th>Product</th>
                    <th>Stars</th>
                    <th>Qty</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Actions</th>
                </tr>
            </thead>

            <tbody>
                ${orders.map((order) => `
                    <tr>
                        <td>
                            #${escapeHTML(
                                order.id
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                order.username ||
                                order.telegram_id ||
                                order.user_id ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                order.product_name ||
                                order.product ||
                                "-"
                            )}
                        </td>

                        <td>
                            ⭐ ${escapeHTML(
                                order.total_stars ??
                                order.amount_stars ??
                                0
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                order.quantity ??
                                1
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                order.status ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                order.created_at ||
                                "-"
                            )}
                        </td>

                        <td>
                            <button
                                class="btn tiny"
                                onclick="viewOrderDetails(${Number(order.id)})"
                            >
                                View
                            </button>
                        </td>
                    </tr>
                `).join("")}
            </tbody>
        </table>
    `;
}

/* =========================================================
   ORDER DETAILS
   ========================================================= */

async function viewOrderDetails(orderId) {
    try {
        const data =
            await apiFetch(
                `${API}/orders/${Number(orderId)}`
            );

        const order =
            data?.order;

        if (!order) {
            throw new Error(
                "Order not found."
            );
        }

        const items =
            Array.isArray(
                data.items
            )
                ? data.items
                : [];

        const payments =
            Array.isArray(
                data.payments
            )
                ? data.payments
                : [];

        const deliveries =
            Array.isArray(
                data.deliveries
            )
                ? data.deliveries
                : [];

        const customer =
            [
                order.first_name,
                order.last_name
            ]
                .filter(Boolean)
                .join(" ") ||
            "-";

        const username =
            order.username
                ? `@${String(
                    order.username
                ).replace(
                    /^@/,
                    ""
                )}`
                : "-";

        const itemsHTML =
            items.length
                ? `
                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Product</th>
                                    <th>Qty</th>
                                    <th>Unit Stars</th>
                                    <th>Total Stars</th>
                                </tr>
                            </thead>

                            <tbody>
                                ${items.map((item) => `
                                    <tr>
                                        <td>
                                            ${escapeHTML(
                                                item.product_name ||
                                                `Product #${item.product_id}`
                                            )}
                                        </td>

                                        <td>
                                            ${escapeHTML(
                                                item.quantity ??
                                                0
                                            )}
                                        </td>

                                        <td>
                                            ⭐ ${escapeHTML(
                                                item.unit_price_stars ??
                                                0
                                            )}
                                        </td>

                                        <td>
                                            ⭐ ${escapeHTML(
                                                item.total_stars ??
                                                0
                                            )}
                                        </td>
                                    </tr>
                                `).join("")}
                            </tbody>
                        </table>
                    </div>
                `
                : `
                    <div class="empty">
                        No order items.
                    </div>
                `;

        const paymentsHTML =
            payments.length
                ? `
                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Amount</th>
                                    <th>Status</th>
                                    <th>Telegram Charge</th>
                                    <th>Provider Charge</th>
                                    <th>Created</th>
                                </tr>
                            </thead>

                            <tbody>
                                ${payments.map((payment) => `
                                    <tr>
                                        <td>
                                            #${escapeHTML(
                                                payment.id
                                            )}
                                        </td>

                                        <td>
                                            ⭐ ${escapeHTML(
                                                payment.amount_stars ??
                                                0
                                            )}
                                        </td>

                                        <td>
                                            ${escapeHTML(
                                                payment.status ||
                                                "-"
                                            )}
                                        </td>

                                        <td>
                                            <code>
                                                ${escapeHTML(
                                                    payment.telegram_payment_charge_id ||
                                                    "-"
                                                )}
                                            </code>
                                        </td>

                                        <td>
                                            <code>
                                                ${escapeHTML(
                                                    payment.provider_payment_charge_id ||
                                                    "-"
                                                )}
                                            </code>
                                        </td>

                                        <td>
                                            ${escapeHTML(
                                                payment.created_at ||
                                                "-"
                                            )}
                                        </td>
                                    </tr>
                                `).join("")}
                            </tbody>
                        </table>
                    </div>
                `
                : `
                    <div class="empty">
                        No payment records.
                    </div>
                `;

        const deliveriesHTML =
            deliveries.length
                ? `
                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Data</th>
                                    <th>Created</th>
                                </tr>
                            </thead>

                            <tbody>
                                ${deliveries.map((delivery) => `
                                    <tr>
                                        <td>
                                            #${escapeHTML(
                                                delivery.id
                                            )}
                                        </td>

                                        <td>
                                            <code>
                                                ${escapeHTML(
                                                    delivery.item_data ||
                                                    delivery.delivery_data ||
                                                    delivery.data ||
                                                    JSON.stringify(
                                                        delivery
                                                    )
                                                )}
                                            </code>
                                        </td>

                                        <td>
                                            ${escapeHTML(
                                                delivery.created_at ||
                                                "-"
                                            )}
                                        </td>
                                    </tr>
                                `).join("")}
                            </tbody>
                        </table>
                    </div>
                `
                : `
                    <div class="empty">
                        No delivery records.
                    </div>
                `;

        const details =
            $("orderDetailsContent");

        if (!details) {
            throw new Error(
                "Order details container is missing."
            );
        }

        details.innerHTML = `
            <div class="detail-grid">

                <div>
                    <span class="muted">
                        Order ID
                    </span>

                    <strong>
                        #${escapeHTML(
                            order.id
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Status
                    </span>

                    <strong>
                        ${escapeHTML(
                            order.status ||
                            "-"
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Customer
                    </span>

                    <strong>
                        ${escapeHTML(
                            customer
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Username
                    </span>

                    <strong>
                        ${escapeHTML(
                            username
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Telegram ID
                    </span>

                    <strong>
                        ${escapeHTML(
                            order.telegram_id ||
                            "-"
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        User ID
                    </span>

                    <strong>
                        ${escapeHTML(
                            order.user_id ||
                            "-"
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Subtotal
                    </span>

                    <strong>
                        ⭐ ${escapeHTML(
                            order.subtotal_stars ??
                            order.total_stars ??
                            0
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Discount
                    </span>

                    <strong>
                        ⭐ ${escapeHTML(
                            order.discount_stars ??
                            0
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Total
                    </span>

                    <strong>
                        ⭐ ${escapeHTML(
                            order.total_stars ??
                            0
                        )}
                    </strong>
                </div>

                <div>
                    <span class="muted">
                        Created
                    </span>

                    <strong>
                        ${escapeHTML(
                            order.created_at ||
                            "-"
                        )}
                    </strong>
                </div>

            </div>

            <div class="detail-section">
                <h3>
                    Order Items
                </h3>

                ${itemsHTML}
            </div>

            <div class="detail-section">
                <h3>
                    Payments
                </h3>

                ${paymentsHTML}
            </div>

            <div class="detail-section">
                <h3>
                    Deliveries
                </h3>

                ${deliveriesHTML}
            </div>
        `;

        openModal(
            "orderDetailsModal"
        );
    } catch (error) {
        console.error(
            "[ADMIN] Order detail error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}

/* =========================================================
   PAYMENTS
   ========================================================= */

async function loadPayments() {
    const box =
        $("paymentsTable");

    setContainerLoading(
        "paymentsTable"
    );

    try {
        const data =
            await apiFetch(
                `${API}/payments`
            );

        currentPayments =
            normalizeList(
                data,
                [
                    "payments",
                    "items"
                ]
            );

        renderPayments();
    } catch (error) {
        console.error(
            "[ADMIN] Payments error:",
            error
        );

        if (box) {
            box.innerHTML = `
                <div class="empty error">
                    ${escapeHTML(
                        error.message
                    )}
                </div>
            `;
        }
    }
}

function renderPayments() {
    const box =
        $("paymentsTable");

    if (!box) return;

    const search =
        $("paymentSearch")
            ?.value
            ?.trim()
            .toLowerCase() ||
        "";

    let payments = [...currentPayments];

    if (search) {
        payments = payments.filter((payment) => {
            const haystack = [
                payment.id,
                payment.user_id,
                payment.telegram_id,
                payment.username,
                payment.telegram_payment_charge_id,
                payment.provider_payment_charge_id,
                payment.status
            ]
                .join(" ")
                .toLowerCase();

            return haystack.includes(search);
        });
    }

    if (!payments.length) {
        box.innerHTML = `<div class="empty">No payments available</div>`;
        return;
    }

    box.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>User</th>
                    <th>Stars</th>
                    <th>Status</th>
                    <th>Telegram Charge</th>
                    <th>Provider Charge</th>
                    <th>Created</th>
                </tr>
            </thead>
            <tbody>
                ${payments.map((payment) => `
                    <tr>
                        <td>#${escapeHTML(payment.id)}</td>
                        <td>${escapeHTML(
                            payment.username ||
                            payment.telegram_id ||
                            payment.user_id ||
                            "-"
                        )}</td>
                        <td>⭐ ${escapeHTML(
                            payment.amount_stars ?? 0
                        )}</td>
                        <td>
                            <span class="badge ${statusClass(payment.status)}">
                                ${escapeHTML(payment.status || "-")}
                            </span>
                        </td>
                        <td>${escapeHTML(
                            payment.telegram_payment_charge_id || "-"
                        )}</td>
                        <td>${escapeHTML(
                            payment.provider_payment_charge_id || "-"
                        )}</td>
                        <td>${escapeHTML(
                            formatDate(payment.created_at)
                        )}</td>
                    </tr>
                `).join("")}
            </tbody>
        </table>
    `;
}

/* =========================================================
   USERS
   ========================================================= */

async function loadUsers() {
    const box = $("usersTable");

    setContainerLoading("usersTable");

    try {
        const data =
            await apiFetch(`${API}/users`);

        currentUsers =
            normalizeList(
                data,
                ["users", "items"]
            );

        renderUsers();
    } catch (error) {
        console.error(
            "[ADMIN] Users error:",
            error
        );

        if (box) {
            box.innerHTML = `
                <div class="empty error">
                    ${escapeHTML(error.message)}
                </div>
            `;
        }
    }
}

function renderUsers() {
    const box = $("usersTable");

    if (!box) return;

    const search =
        $("userSearch")
            ?.value
            ?.trim()
            .toLowerCase() ||
        "";

    let users =
        [...currentUsers];

    if (search) {
        users =
            users.filter((user) => {
                const haystack = [
                    user.id,
                    user.telegram_id,
                    user.username,
                    user.first_name,
                    user.last_name
                ]
                    .join(" ")
                    .toLowerCase();

                return haystack.includes(search);
            });
    }

    if (!users.length) {
        box.innerHTML =
            `<div class="empty">No users available</div>`;
        return;
    }

    box.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Telegram ID</th>
                    <th>Username</th>
                    <th>First Name</th>
                    <th>Last Name</th>
                    <th>Created</th>
                </tr>
            </thead>
            <tbody>
                ${users.map((user) => `
                    <tr>
                        <td>
                            ${escapeHTML(user.id)}
                        </td>

                        <td>
                            ${escapeHTML(
                                user.telegram_id ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                user.username ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                user.first_name ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                user.last_name ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                formatDate(
                                    user.created_at
                                )
                            )}
                        </td>
                    </tr>
                `).join("")}
            </tbody>
        </table>
    `;
}

/* =========================================================
   CREATE GAME / CATEGORY
   ========================================================= */

async function saveGame(event) {
    event.preventDefault();
    const id = Number($("gameId")?.value || 0);
    const name = $("gameName")?.value?.trim() || "";
    const description = $("gameDescription")?.value?.trim() || "";
    const image = $("gameImage")?.value?.trim() || "";
    const active = $("gameActive")?.checked !== false;
    const sort_order = Number($("gameSort")?.value || 0);
    if (!name) return showMessage("Game name is required.", "error");
    try {
        await apiFetch(id ? `${API}/games/${id}` : `${API}/games`, {
            method: id ? "PUT" : "POST",
            body: JSON.stringify({name, description, image, active, sort_order})
        });
        closeModal("gameModal");
        $("gameForm")?.reset();
        await loadGames();
        await loadCategories();
        await loadDashboard();
    } catch (error) { showMessage(error.message, "error"); }
}

function editGame(id) {
    const game = findGame(id);
    if (!game) return;
    $("gameId").value = game.id;
    $("gameName").value = game.name || "";
    $("gameDescription").value = game.description || "";
    $("gameImage").value = game.image || "";
    $("gameActive").checked = game.active !== false && game.active !== 0;
    $("gameSort").value = game.sort_order ?? 0;
    $("gameModalTitle").textContent = "Edit Game";
    openModal("gameModal");
}

function openCategoryModal(categoryId = 0) {
    populateGameSelects();
    const form = $("categoryForm");
    if (form) form.reset();
    $("categoryId").value = categoryId || "";
    if (categoryId) {
        const c = findCategory(categoryId);
        if (!c) return;
        $("categoryGame").value = c.game_id || "";
        $("categoryName").value = c.name || "";
        $("categoryDescription").value = c.description || "";
        $("categoryImage").value = c.image || "";
        $("categoryActive").checked = c.active !== false && c.active !== 0;
        $("categorySort").value = c.sort_order ?? 0;
        $("categoryModalTitle").textContent = "Edit Category";
    } else {
        $("categoryModalTitle").textContent = "Add Category";
        $("categoryActive").checked = true;
        $("categorySort").value = 0;
    }
    openModal("categoryModal");
}

function editCategory(id) { openCategoryModal(id); }

async function saveCategory(event) {
    event.preventDefault();
    const id = Number($("categoryId")?.value || 0);
    const gameId = Number($("categoryGame")?.value || 0);
    const name = $("categoryName")?.value?.trim() || "";
    const description = $("categoryDescription")?.value?.trim() || "";
    const image = $("categoryImage")?.value?.trim() || "";
    const active = $("categoryActive")?.checked !== false;
    const sort_order = Number($("categorySort")?.value || 0);
    if (!gameId) return showMessage("Please select a game.", "error");
    if (!name) return showMessage("Category name is required.", "error");
    try {
        await apiFetch(id ? `${API}/categories/${id}` : `${API}/categories`, {
            method: id ? "PUT" : "POST",
            body: JSON.stringify({game_id:gameId, name, description, image, active, sort_order})
        });
        closeModal("categoryModal");
        $("categoryForm")?.reset();
        await loadCategories();
        await loadProducts();
        await loadDashboard();
    } catch (error) { showMessage(error.message, "error"); }
}

/* =========================================================
   TEST STORE + REFERRAL SETTINGS
   ========================================================= */

function populateTestOrderProducts() {
    const select = $("testOrderProduct");
    if (!select) return;
    const active = currentProducts.filter(p => p.active !== false);
    select.innerHTML = active.length
        ? active.map(p => `<option value="${escapeHTML(p.id)}">${escapeHTML(p.name)} · ⭐ ${escapeHTML(p.price_stars ?? 0)} · Stock ${escapeHTML(p.stock ?? 0)}</option>`).join("")
        : `<option value="">No active products</option>`;
}

async function createTestOrder() {
    const button = $("createTestOrderBtn");
    const message = $("testOrderMessage");
    const productId = Number($("testOrderProduct")?.value || 0);
    const quantity = Number($("testOrderQuantity")?.value || 1);
    const complete = $("testOrderComplete")?.checked !== false;
    if (!productId) return showMessage("Please select a product.", "error");
    if (!Number.isInteger(quantity) || quantity < 1) return showMessage("Quantity must be at least 1.", "error");
    try {
        if (button) { button.disabled = true; button.textContent = "Creating…"; }
        const data = await apiFetch(`${API}/test-order`, {
            method: "POST",
            body: JSON.stringify({product_id: productId, quantity, complete})
        });
        const o = data.order || {};
        if (message) {
            message.className = "form-message success";
            message.textContent = `✅ Test order #${o.order_id} created as ${o.status}.`;
        }
        await loadOrders();
        await loadProducts();
        populateTestOrderProducts();
        if (complete && Array.isArray(o.inventory_items) && o.inventory_items.length) {
            showMessage(`Test order #${o.order_id} delivered to Admin.`, "success");
        }
    } catch (error) {
        if (message) { message.className = "form-message error"; message.textContent = error.message; }
        else showMessage(error.message, "error");
    } finally {
        if (button) { button.disabled = false; button.textContent = "🧪 Create Test Order"; }
    }
}

function updateReferralPreview() {
    const per = Number($("refPerUser")?.value || 0);
    const max = Number($("refMaxDiscount")?.value || 0);
    const days = Number($("refDays")?.value || 0);
    const x = $("referralPreview");
    if (x) x.textContent = `${per}% per referral • up to ${max}% • active for ${days} days`;
}

async function loadReferralSettings() {
    try {
        const data = await apiFetch(`${API}/referral-settings`);
        const s = data.settings || {};
        $("refPerUser").value = Number(s.per_user ?? 1);
        $("refMaxDiscount").value = Number(s.max_discount ?? 70);
        $("refDays").value = Number(s.days ?? 7);
        updateReferralPreview();
        const msg = $("referralSettingsMessage");
        if (msg) { msg.className = "form-message"; msg.textContent = "Settings loaded."; }
    } catch (error) {
        showMessage(`Referral settings error: ${error.message}`, "error");
    }
}

async function saveReferralSettings() {
    const button = $("saveReferralSettingsBtn");
    const msg = $("referralSettingsMessage");
    try {
        const per = Number($("refPerUser")?.value || 0);
        const max = Number($("refMaxDiscount")?.value || 0);
        const days = Number($("refDays")?.value || 0);
        if (!Number.isInteger(per) || per < 0 || per > 100) throw new Error("Per-referral discount must be 0-100.");
        if (!Number.isInteger(max) || max < 0 || max > 100) throw new Error("Maximum discount must be 0-100.");
        if (!Number.isInteger(days) || days < 0 || days > 3650) throw new Error("Duration must be 0-3650 days.");
        if (button) { button.disabled = true; button.textContent = "Saving…"; }
        const data = await apiFetch(`${API}/referral-settings`, {
            method: "PUT",
            body: JSON.stringify({per_user: per, max_discount: max, days})
        });
        const s = data.settings || {};
        $("refPerUser").value = Number(s.per_user ?? per);
        $("refMaxDiscount").value = Number(s.max_discount ?? max);
        $("refDays").value = Number(s.days ?? days);
        updateReferralPreview();
        if (msg) { msg.className = "form-message success"; msg.textContent = "✅ Referral settings saved."; }
    } catch (error) {
        if (msg) { msg.className = "form-message error"; msg.textContent = error.message; }
        else showMessage(error.message, "error");
    } finally {
        if (button) { button.disabled = false; button.textContent = "Save Referral Settings"; }
    }
}

/* =========================================================
   FILTER EVENTS
   ========================================================= */

function setupFilters() {
    $("createTestOrderBtn")?.addEventListener("click", createTestOrder);
    $("saveReferralSettingsBtn")?.addEventListener("click", saveReferralSettings);
    ["refPerUser","refMaxDiscount","refDays"].forEach(id => $(id)?.addEventListener("input", updateReferralPreview));

    $("productSearch")?.addEventListener(
        "input",
        renderProducts
    );

    $("productGameFilter")?.addEventListener(
        "change",
        renderProducts
    );

    $("productCategoryFilter")?.addEventListener(
        "change",
        renderProducts
    );

    $("productStatusFilter")?.addEventListener(
        "change",
        renderProducts
    );

    $("inventoryProductFilter")?.addEventListener(
        "change",
        loadInventory
    );

    $("inventoryStatusFilter")?.addEventListener(
        "change",
        loadInventory
    );

    $("inventorySearch")?.addEventListener(
        "input",
        loadInventory
    );

    $("inventoryBulkDeleteBtn")?.addEventListener(
        "click",
        deleteSelectedInventory
    );

    $("inventoryItemData")?.addEventListener(
        "input",
        updateInventoryInputCount
    );

    $("inventorySelectAll")?.addEventListener(
        "change",
        () =>
            toggleAllInventory(
                $("inventorySelectAll").checked
            )
    );

    $("orderSearch")?.addEventListener(
        "input",
        renderOrders
    );

    $("orderStatusFilter")?.addEventListener(
        "change",
        renderOrders
    );

    $("paymentSearch")?.addEventListener(
        "input",
        renderPayments
    );

    $("userSearch")?.addEventListener(
        "input",
        renderUsers
    );

    $("categoryGameFilter")?.addEventListener(
        "change",
        loadCategories
    );
}

/* =========================================================
   MODALS / FORMS
   ========================================================= */

function setupModals() {
    $("storeStatusOnline")?.addEventListener("click", () => setStoreStatus(true));
    $("storeStatusOffline")?.addEventListener("click", () => setStoreStatus(false));
    $("storeStatusToggle")?.addEventListener("click", toggleStoreStatus);
    document.querySelectorAll(".modal").forEach((modal) => {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) {
                closeModal(modal.id);
            }
        });
    });

    const gameForm = $("gameForm");
    if (gameForm) {
        gameForm.addEventListener("submit", saveGame);
    }

    const categoryForm = $("categoryForm");
    if (categoryForm) {
        categoryForm.addEventListener("submit", saveCategory);
    }

    const productForm = $("productForm");
    if (productForm) {
        productForm.addEventListener("submit", saveProduct);
    }

    $("productInventoryData")?.addEventListener("input", updateProductInventoryInputCount);
    $("productInventoryAddBtn")?.addEventListener("click", addInventoryFromProductStep);
    $("productInventoryFinishBtn")?.addEventListener("click", finishProductSetup);

    const inventoryForm = $("inventoryForm");
    if (inventoryForm) {
        inventoryForm.addEventListener("submit", saveInventory);
    }
}

/* =========================================================
   GLOBALS FOR EXISTING INLINE HTML
   ========================================================= */

window.openModal = openModal;
window.closeModal = closeModal;
window.backdropClose = backdropClose;
window.openProductModal = openProductModal;
window.editProduct = editProduct;
window.viewProductDetails = viewProductDetails;
window.deleteProduct = deleteProduct;
window.toggleProduct = toggleProduct;
window.openCategoryModal = openCategoryModal;
window.editGame = editGame;
window.editCategory = editCategory;
window.openInventoryModal = openInventoryModal;
window.toggleInventorySelection = toggleInventorySelection;
window.toggleAllInventory = toggleAllInventory;
window.deleteInventoryItem = deleteInventoryItem;
window.changeInventoryStatus = changeInventoryStatus;
window.deleteSelectedInventory = deleteSelectedInventory;
window.updateInventoryInputCount = updateInventoryInputCount;
window.updateProductInventoryInputCount = updateProductInventoryInputCount;
window.recheckInventoryPreview = recheckInventoryPreview;
window.confirmInventoryPreview = confirmInventoryPreview;
window.closeInventoryPreview = closeInventoryPreview;
window.addInventoryFromProductStep = addInventoryFromProductStep;
window.finishProductSetup = finishProductSetup;
window.loadDashboard = loadDashboard;
window.loadGames = loadGames;
window.loadCategories = loadCategories;
window.loadProducts = loadProducts;
window.loadInventory = loadInventory;
window.loadOrders = loadOrders;
window.renderOrders = renderOrders;
window.viewOrderDetails = viewOrderDetails;
window.loadPayments = loadPayments;
window.renderPayments = renderPayments;
window.loadUsers = loadUsers;
window.renderUsers = renderUsers;
window.renderProducts = renderProducts;
window.logout = logout;
window.populateTestOrderProducts = populateTestOrderProducts;
window.createTestOrder = createTestOrder;
window.loadReferralSettings = loadReferralSettings;
window.saveReferralSettings = saveReferralSettings;

/* =========================================================
   INITIALIZATION
   ========================================================= */

async function initAdmin() {
    console.log("[ADMIN] Stage 11.2 product management loaded.");

    const authenticated = await checkAuth();

    if (!authenticated) {
        console.error("[ADMIN] Authentication failed.");
        return;
    }

    setupNavigation();
    setupFilters();
    setupModals();

    setContainerLoading("recentOrders");
    setContainerLoading("productsTable");
    setContainerLoading("gamesTable");
    setContainerLoading("categoriesTable");

    const results = await Promise.allSettled([
        loadGames(),
        loadCategories(),
        loadProducts(),
        loadDashboard()
    ]);

    console.log(
        "[ADMIN] Initial loads:",
        results.map((result) => result.status)
    );

    // Keep the dashboard visible after initialization.
    switchSection("dashboard");

    console.log("[ADMIN] Initialization finished.");
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAdmin, {
        once: true
    });
} else {
    initAdmin();
}
