/* =========================================================
   YOUR SHOP - ADMIN PANEL
   Unified version for current admin/index.html
   ========================================================= */

"use strict";

const API = "/api/admin";

let currentProducts = [];
let currentGames = [];
let currentCategories = [];
let currentInventory = [];
let currentOrders = [];
let currentPayments = [];
let currentUsers = [];

/* =========================================================
   HELPERS
   ========================================================= */

function $(id) {
    return document.getElementById(id);
}

function escapeHTML(value) {
    if (value === null || value === undefined) {
        return "";
    }

    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function normalizeList(data, keys = []) {
    if (Array.isArray(data)) {
        return data;
    }

    for (const key of keys) {
        if (Array.isArray(data?.[key])) {
            return data[key];
        }
    }

    return [];
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

    clearTimeout(showMessage.timer);

    showMessage.timer = setTimeout(() => {
        box.style.display = "none";
    }, 4000);
}

function setStatus(text, type = "") {
    const el = $("statusBadge");

    if (!el) {
        return;
    }

    el.textContent = text;
    el.className = `status ${type}`.trim();
}

function setLoading(id, text = "Loading…") {
    const el = $(id);

    if (!el) {
        return;
    }

    el.innerHTML = `
        <div class="empty">
            ${escapeHTML(text)}
        </div>
    `;
}

/* =========================================================
   API
   ========================================================= */

async function apiFetch(url, options = {}) {
    const controller = new AbortController();

    const timeout = setTimeout(() => {
        controller.abort();
    }, 15000);

    try {
        const response = await fetch(url, {
            credentials: "include",
            cache: "no-store",
            signal: controller.signal,
            ...options,

            headers: {
                "Accept": "application/json",

                ...(options.body
                    ? {
                        "Content-Type":
                            "application/json"
                    }
                    : {}),

                ...(options.headers || {})
            }
        });

        const text = await response.text();

        let data = {};

        if (text) {
            try {
                data = JSON.parse(text);
            } catch (_) {
                data = {
                    raw: text
                };
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
            throw new Error(
                "Request timed out."
            );
        }

        throw error;

    } finally {
        clearTimeout(timeout);
    }
}

/* =========================================================
   MODALS
   ========================================================= */

function openModal(id) {
    const modal = $(id);

    if (!modal) {
        console.error(
            `[ADMIN] Modal not found: ${id}`
        );
        return;
    }

    modal.classList.add("active");
    modal.style.display = "flex";

    const backdrop = $("modalBackdrop");

    if (backdrop) {
        backdrop.classList.add("active");
        backdrop.style.display = "block";
    }
}

function closeModal(id) {
    const modal = $(id);

    if (!modal) {
        return;
    }

    modal.classList.remove("active");
    modal.style.display = "none";

    const anyOpen =
        document.querySelector(
            ".modal.active"
        );

    if (!anyOpen) {
        const backdrop =
            $("modalBackdrop");

        if (backdrop) {
            backdrop.classList.remove("active");
            backdrop.style.display = "none";
        }
    }
}

function backdropClose(event) {
    if (
        event.target !==
        event.currentTarget
    ) {
        return;
    }

    document
        .querySelectorAll(".modal.active")
        .forEach((modal) => {
            closeModal(modal.id);
        });
}

/* =========================================================
   AUTH
   ========================================================= */

async function checkAuth() {
    try {

        setStatus(
            "Checking session…"
        );

        console.log(
            "[ADMIN] Checking session..."
        );

        const data =
            await apiFetch(
                `${API}/me`
            );

        console.log(
            "[ADMIN] /me response:",
            data
        );

        const authenticated =
            data?.authenticated === true ||
            data?.authenticated === 1 ||
            data?.ok === true ||
            data?.admin === true ||
            data?.is_admin === true;

        if (!authenticated) {

            setStatus(
                "Not authenticated",
                "danger"
            );

            showMessage(
                "Your admin session is not active.",
                "error"
            );

            return false;
        }

        setStatus(
            "Session active",
            "success"
        );

        console.log(
            "[ADMIN] Authentication successful."
        );

        return true;

    } catch (error) {

        console.error(
            "[ADMIN] Auth error:",
            error
        );

        setStatus(
            "Session error",
            "danger"
        );

        showMessage(
            `Authentication error: ${error.message}`,
            "error"
        );

        return false;
    }
}

async function logout() {

    try {

        await apiFetch(
            `${API}/logout`,
            {
                method: "POST"
            }
        );

    } catch (error) {

        console.error(
            "[ADMIN] Logout error:",
            error
        );

    } finally {

        window.location.href =
            "/admin";
    }
}

/* =========================================================
   NAVIGATION
   ========================================================= */

const SECTION_INFO = {
    dashboard: [
        "Dashboard",
        "Store overview"
    ],

    games: [
        "Games",
        "Manage games"
    ],

    categories: [
        "Categories",
        "Manage categories"
    ],

    products: [
        "Products",
        "Manage products"
    ],

    inventory: [
        "Inventory",
        "Manage delivery inventory"
    ],

    orders: [
        "Orders",
        "View customer orders"
    ],

    payments: [
        "Payments",
        "View successful payments"
    ],

    users: [
        "Users",
        "View registered users"
    ]
};

function switchSection(name) {

    document
        .querySelectorAll(".section")
        .forEach((section) => {

            section.classList.remove(
                "active"
            );

        });

    const target =
        $(`section-${name}`);

    if (target) {
        target.classList.add("active");
    }

    document
        .querySelectorAll(
            "[data-section]"
        )
        .forEach((button) => {

            button.classList.toggle(
                "active",
                button.dataset.section ===
                name
            );

        });

    const info =
        SECTION_INFO[name] ||
        SECTION_INFO.dashboard;

    const title =
        $("pageTitle");

    const subtitle =
        $("pageSubtitle");

    if (title) {
        title.textContent =
            info[0];
    }

    if (subtitle) {
        subtitle.textContent =
            info[1];
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

    if (name === "orders") {
        loadOrders();
    }

    if (name === "payments") {
        loadPayments();
    }

    if (name === "users") {
        loadUsers();
    }
}

function setupNavigation() {

    document
        .querySelectorAll(
            "[data-section]"
        )
        .forEach((button) => {

            button.addEventListener(
                "click",
                () => {
                    switchSection(
                        button.dataset.section
                    );
                }
            );

        });

    const logoutBtn =
        $("logoutBtn");

    if (logoutBtn) {

        logoutBtn.addEventListener(
            "click",
            logout
        );

    }
}

/* =========================================================
   DASHBOARD
   ========================================================= */

async function loadDashboard() {

    try {

        const data =
            await apiFetch(
                `${API}/stats`
            );

        console.log(
            "[ADMIN] Stats:",
            data
        );

        const stats =
            data?.stats ||
            data ||
            {};

        const products =
            stats.total_products ??
            stats.products ??
            0;

        const games =
            stats.total_games ??
            stats.games ??
            0;

        const categories =
            stats.total_categories ??
            stats.categories ??
            0;

        const users =
            stats.total_users ??
            stats.users ??
            0;

        const orders =
            stats.total_orders ??
            stats.orders ??
            0;

        const revenue =
            stats.revenue_stars ??
            stats.total_revenue ??
            stats.total_paid_stars ??
            stats.revenue ??
            0;

        if ($("statProducts")) {
            $("statProducts").textContent =
                products;
        }

        if ($("statGames")) {
            $("statGames").textContent =
                games;
        }

        if ($("statCategories")) {
            $("statCategories").textContent =
                categories;
        }

        if ($("statUsers")) {
            $("statUsers").textContent =
                users;
        }

        if ($("statOrders")) {
            $("statOrders").textContent =
                orders;
        }

        if ($("statRevenue")) {
            $("statRevenue").textContent =
                revenue;
        }

        await loadRecentOrders();

    } catch (error) {

        console.error(
            "[ADMIN] Dashboard error:",
            error
        );

        showMessage(
            `Dashboard error: ${error.message}`,
            "error"
        );
    }
}

async function loadRecentOrders() {

    const box =
        $("recentOrders");

    if (!box) {
        return;
    }

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

        const recent =
            currentOrders.slice(
                0,
                5
            );

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

                ${recent.map(
                    (order) => `

                    <div class="mini-order">

                        <div>
                            <strong>
                                #${escapeHTML(
                                    order.id
                                )}
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
                `
                ).join("")}

            </div>
        `;

    } catch (error) {

        console.error(
            "[ADMIN] Recent orders error:",
            error
        );

        box.innerHTML = `
            <div class="empty error">
                ${escapeHTML(
                    error.message
                )}
            </div>
        `;
    }
}

/* =========================================================
   GAMES
   ========================================================= */

async function loadGames() {

    const box =
        $("gamesTable");

    setLoading(
        "gamesTable"
    );

    try {

        const data =
            await apiFetch(
                `${API}/games`
            );

        console.log(
            "[ADMIN] Games:",
            data
        );

        currentGames =
            normalizeList(
                data,
                [
                    "games",
                    "items"
                ]
            );

        renderGames();

        populateGameSelects();
        populateProductGameFilter();
        populateCategoryGameFilter();
        populateInventoryProductSelect();

    } catch (error) {

        console.error(
            "[ADMIN] Games error:",
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

function renderGames() {

    const box =
        $("gamesTable");

    if (!box) {
        return;
    }

    if (!currentGames.length) {

        box.innerHTML = `
            <div class="empty">
                No games available
            </div>
        `;

        return;
    }

    box.innerHTML = `
        <table>

            <thead>
                <tr>
                    <th>ID</th>
                    <th>Name</th>
                    <th>Slug</th>
                    <th>Created</th>
                </tr>
            </thead>

            <tbody>

                ${currentGames.map(
                    (game) => `

                    <tr>

                        <td>
                            ${escapeHTML(
                                game.id
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                game.name ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                game.slug ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                game.created_at ||
                                "-"
                            )}
                        </td>

                    </tr>

                `
                ).join("")}

            </tbody>

        </table>
    `;
}

function populateGameSelects() {

    const selects = [
        "categoryGame",
        "productGame"
    ];

    selects.forEach((id) => {

        const select =
            $(id);

        if (!select) {
            return;
        }

        const current =
            select.value;

        select.innerHTML = `
            <option value="">
                Select game
            </option>
        `;

        currentGames.forEach(
            (game) => {

                const option =
                    document.createElement(
                        "option"
                    );

                option.value =
                    game.id;

                option.textContent =
                    game.name ||
                    `Game ${game.id}`;

                select.appendChild(
                    option
                );

            }
        );

        if (current) {
            select.value =
                current;
        }
    });
}

/* =========================================================
   CATEGORIES
   ========================================================= */

async function loadCategories() {

    const box =
        $("categoriesTable");

    setLoading(
        "categoriesTable"
    );

    try {

        const data =
            await apiFetch(
                `${API}/categories`
            );

        console.log(
            "[ADMIN] Categories:",
            data
        );

        let categories =
            normalizeList(
                data,
                [
                    "categories",
                    "items"
                ]
            );

        const gameFilter =
            $("categoryGameFilter")
                ?.value ||
            "";

        if (gameFilter) {

            categories =
                categories.filter(
                    (category) =>
                        String(
                            category.game_id ??
                            ""
                        ) ===
                        String(
                            gameFilter
                        )
                );
        }

        currentCategories =
            categories;

        renderCategories();

        populateCategorySelect();
        populateProductCategoryFilter();

    } catch (error) {

        console.error(
            "[ADMIN] Categories error:",
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

function renderCategories() {

    const box =
        $("categoriesTable");

    if (!box) {
        return;
    }

    if (!currentCategories.length) {

        box.innerHTML = `
            <div class="empty">
                No categories available
            </div>
        `;

        return;
    }

    box.innerHTML = `
        <table>

            <thead>
                <tr>
                    <th>ID</th>
                    <th>Name</th>
                    <th>Slug</th>
                    <th>Game</th>
                </tr>
            </thead>

            <tbody>

                ${currentCategories.map(
                    (category) => `

                    <tr>

                        <td>
                            ${escapeHTML(
                                category.id
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                category.name ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                category.slug ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                category.game_name ||
                                findGame(
                                    category.game_id
                                )?.name ||
                                "-"
                            )}
                        </td>

                    </tr>

                `
                ).join("")}

            </tbody>

        </table>
    `;
}

function populateCategorySelect() {

    const select =
        $("productCategory");

    if (!select) {
        return;
    }

    const current =
        select.value;

    select.innerHTML = `
        <option value="">
            Select category
        </option>
    `;

    currentCategories.forEach(
        (category) => {

            const option =
                document.createElement(
                    "option"
                );

            option.value =
                category.id;

            option.textContent =
                category.name ||
                `Category ${category.id}`;

            select.appendChild(
                option
            );

        }
    );

    if (current) {
        select.value =
            current;
    }
}

function populateCategoryGameFilter() {

    const select =
        $("categoryGameFilter");

    if (!select) {
        return;
    }

    const current =
        select.value;

    select.innerHTML = `
        <option value="">
            All games
        </option>
    `;

    currentGames.forEach(
        (game) => {

            const option =
                document.createElement(
                    "option"
                );

            option.value =
                game.id;

            option.textContent =
                game.name ||
                `Game ${game.id}`;

            select.appendChild(
                option
            );

        }
    );

    if (current) {
        select.value =
            current;
    }
}

/* =========================================================
   PRODUCTS
   ========================================================= */

async function loadProducts() {

    const box =
        $("productsTable");

    setLoading(
        "productsTable"
    );

    try {

        const data =
            await apiFetch(
                `${API}/products`
            );

        console.log(
            "[ADMIN] Products:",
            data
        );

        currentProducts =
            normalizeList(
                data,
                [
                    "products",
                    "items"
                ]
            );

        populateProductGameFilter();
        populateProductCategoryFilter();
        populateInventoryProductSelect();

        renderProducts();
        renderLowStock();

    } catch (error) {

        console.error(
            "[ADMIN] Products error:",
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

function renderProducts() {

    const box =
        $("productsTable");

    if (!box) {
        return;
    }

    const search =
        $("productSearch")
            ?.value
            ?.trim()
            .toLowerCase() ||
        "";

    const gameFilter =
        $("productGameFilter")
            ?.value ||
        "";

    const categoryFilter =
        $("productCategoryFilter")
            ?.value ||
        "";

    let products =
        [...currentProducts];

    if (search) {

        products =
            products.filter(
                (product) => {

                    const text = [
                        product.id,
                        product.name,
                        product.game_name,
                        product.category_name,
                        product.description
                    ]
                        .join(" ")
                        .toLowerCase();

                    return text.includes(
                        search
                    );
                }
            );
    }

    if (gameFilter) {

        products =
            products.filter(
                (product) =>
                    String(
                        product.game_id ??
                        ""
                    ) ===
                    String(
                        gameFilter
                    )
            );
    }

    if (categoryFilter) {

        products =
            products.filter(
                (product) =>
                    String(
                        product.category_id ??
                        ""
                    ) ===
                    String(
                        categoryFilter
                    )
            );
    }

    if (!products.length) {

        box.innerHTML = `
            <div class="empty">
                No products found
            </div>
        `;

        return;
    }

    box.innerHTML = `
        <table>

            <thead>
                <tr>
                    <th>ID</th>
                    <th>Name</th>
                    <th>Game</th>
                    <th>Category</th>
                    <th>Price</th>
                    <th>Stock</th>
                    <th>Discount</th>
                    <th>Featured</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
            </thead>

            <tbody>

                ${products.map(
                    (product) => {

                        const active =
                            product.active === true ||
                            product.active === 1;

                        const featured =
                            product.featured === true ||
                            product.featured === 1;

                        const discount =
                            Number(
                                product.discount_percent ||
                                0
                            );

                        return `

                            <tr>

                                <td>
                                    ${escapeHTML(
                                        product.id
                                    )}
                                </td>

                                <td>
                                    <strong>
                                        ${escapeHTML(
                                            product.name ||
                                            "-"
                                        )}
                                    </strong>
                                </td>

                                <td>
                                    ${escapeHTML(
                                        product.game_name ||
                                        findGame(
                                            product.game_id
                                        )?.name ||
                                        "-"
                                    )}
                                </td>

                                <td>
                                    ${escapeHTML(
                                        product.category_name ||
                                        findCategory(
                                            product.category_id
                                        )?.name ||
                                        "-"
                                    )}
                                </td>

                                <td>
                                    ⭐ ${escapeHTML(
                                        product.price_stars ??
                                        0
                                    )}
                                </td>

                                <td>
                                    ${escapeHTML(
                                        product.stock ??
                                        0
                                    )}
                                </td>

                                <td>
                                    ${
                                        discount > 0
                                            ? `${discount}%`
                                            : "-"
                                    }
                                </td>

                                <td>
                                    ${
                                        featured
                                            ? "Yes"
                                            : "-"
                                    }
                                </td>

                                <td>
                                    ${
                                        active
                                            ? "Active"
                                            : "Inactive"
                                    }
                                </td>

                                <td>

                                    <div class="actions">

                                        <button
                                            type="button"
                                            class="btn tiny"
                                            onclick="editProduct(${Number(
                                                product.id
                                            )})"
                                        >
                                            Edit
                                        </button>

                                        <button
                                            type="button"
                                            class="btn tiny ${
                                                active
                                                    ? "danger"
                                                    : "success"
                                            }"
                                            onclick="toggleProduct(${Number(
                                                product.id
                                            )}, ${active})"
                                        >
                                            ${
                                                active
                                                    ? "Deactivate"
                                                    : "Activate"
                                            }
                                        </button>

                                    </div>

                                </td>

                            </tr>
                        `;
                    }
                ).join("")}

            </tbody>

        </table>
    `;
}

function findProduct(id) {

    return currentProducts.find(
        (item) =>
            Number(item.id) ===
            Number(id)
    );
}

function findGame(id) {

    return currentGames.find(
        (item) =>
            Number(item.id) ===
            Number(id)
    );
}

function findCategory(id) {

    return currentCategories.find(
        (item) =>
            Number(item.id) ===
            Number(id)
    );
}

function populateProductGameFilter() {

    const select =
        $("productGameFilter");

    if (!select) {
        return;
    }

    const current =
        select.value;

    select.innerHTML = `
        <option value="">
            All games
        </option>
    `;

    currentGames.forEach(
        (game) => {

            const option =
                document.createElement(
                    "option"
                );

            option.value =
                game.id;

            option.textContent =
                game.name ||
                `Game ${game.id}`;

            select.appendChild(
                option
            );

        }
    );

    if (current) {
        select.value =
            current;
    }
}

function populateProductCategoryFilter() {

    const select =
        $("productCategoryFilter");

    if (!select) {
        return;
    }

    const current =
        select.value;

    select.innerHTML = `
        <option value="">
            All categories
        </option>
    `;

    currentCategories.forEach(
        (category) => {

            const option =
                document.createElement(
                    "option"
                );

            option.value =
                category.id;

            option.textContent =
                category.name ||
                `Category ${category.id}`;

            select.appendChild(
                option
            );

        }
    );

    if (current) {
        select.value =
            current;
    }
}

function resetProductForm() {

    const form =
        $("productForm");

    if (form) {
        form.reset();
    }

    if ($("productId")) {
        $("productId").value =
            "";
    }

    if ($("productModalTitle")) {
        $("productModalTitle")
            .textContent =
            "Add Product";
    }

    if ($("productSubmitText")) {
        $("productSubmitText")
            .textContent =
            "Create Product";
    }

    if ($("productFeatured")) {
        $("productFeatured")
            .checked =
            false;
    }

    if ($("productActive")) {
        $("productActive")
            .checked =
            true;
    }

    if ($("productDiscount")) {
        $("productDiscount")
            .value =
            "0";
    }

    if ($("productStock")) {
        $("productStock")
            .value =
            "0";
    }
}

function openProductModal() {

    resetProductForm();

    populateGameSelects();
    populateCategorySelect();

    openModal(
        "productModal"
    );
}

function editProduct(productId) {

    const product =
        findProduct(
            productId
        );

    if (!product) {

        showMessage(
            "Product not found.",
            "error"
        );

        return;
    }

    populateGameSelects();
    populateCategorySelect();

    if ($("productId")) {
        $("productId").value =
            product.id;
    }

    if ($("productName")) {
        $("productName").value =
            product.name ||
            "";
    }

    if ($("productDescription")) {
        $("productDescription")
            .value =
            product.description ||
            "";
    }

    if ($("productGame")) {
        $("productGame").value =
            product.game_id ??
            "";
    }

    if ($("productCategory")) {
        $("productCategory").value =
            product.category_id ??
            "";
    }

    if ($("productPrice")) {
        $("productPrice").value =
            product.price_stars ??
            "";
    }

    if ($("productStock")) {
        $("productStock").value =
            product.stock ??
            0;
    }

    if ($("productDiscount")) {
        $("productDiscount").value =
            product.discount_percent ??
            0;
    }

    if ($("productFeatured")) {
        $("productFeatured")
            .checked =
            product.featured === true ||
            product.featured === 1;
    }

    if ($("productActive")) {
        $("productActive")
            .checked =
            product.active === true ||
            product.active === 1;
    }

    if ($("productBanner")) {
        $("productBanner").value =
            product.banner ||
            "";
    }

    if ($("productModalTitle")) {
        $("productModalTitle")
            .textContent =
            "Edit Product";
    }

    if ($("productSubmitText")) {
        $("productSubmitText")
            .textContent =
            "Save Changes";
    }

    openModal(
        "productModal"
    );
}

async function saveProduct(event) {

    event.preventDefault();

    const id =
        $("productId")
            ?.value
            ?.trim() ||
        "";

    const name =
        $("productName")
            ?.value
            ?.trim() ||
        "";

    const description =
        $("productDescription")
            ?.value
            ?.trim() ||
        "";

    const gameId =
        $("productGame")
            ?.value ||
        "";

    const categoryId =
        $("productCategory")
            ?.value ||
        "";

    const price =
        Number(
            $("productPrice")
                ?.value
        );

    const stock =
        Number(
            $("productStock")
                ?.value
        );

    const discount =
        Number(
            $("productDiscount")
                ?.value ||
            0
        );

    const featured =
        $("productFeatured")
            ?.checked === true;

    const active =
        $("productActive")
            ?.checked === true;

    const banner =
        $("productBanner")
            ?.value
            ?.trim() ||
        "";

    if (!name) {

        showMessage(
            "Product name is required.",
            "error"
        );

        return;
    }

    if (!gameId) {

        showMessage(
            "Please select a game.",
            "error"
        );

        return;
    }

    if (!categoryId) {

        showMessage(
            "Please select a category.",
            "error"
        );

        return;
    }

    if (
        !Number.isFinite(price) ||
        price <= 0
    ) {

        showMessage(
            "Stars price must be greater than 0.",
            "error"
        );

        return;
    }

    if (
        !Number.isFinite(stock) ||
        stock < 0
    ) {

        showMessage(
            "Stock cannot be negative.",
            "error"
        );

        return;
    }

    if (
        !Number.isFinite(discount) ||
        discount < 0 ||
        discount > 100
    ) {

        showMessage(
            "Discount must be between 0 and 100.",
            "error"
        );

        return;
    }

    const payload = {

        name,

        description,

        game_id:
            Number(gameId),

        category_id:
            Number(categoryId),

        price_stars:
            price,

        stock,

        featured,

        active,

        discount_percent:
            discount,

        banner
    };

    try {

        if (id) {

            await apiFetch(
                `${API}/products/${id}`,
                {
                    method: "PUT",
                    body:
                        JSON.stringify(
                            payload
                        )
                }
            );

            showMessage(
                "Product updated successfully."
            );

        } else {

            await apiFetch(
                `${API}/products`,
                {
                    method: "POST",
                    body:
                        JSON.stringify(
                            payload
                        )
                }
            );

            showMessage(
                "Product created successfully."
            );
        }

        closeModal(
            "productModal"
        );

        await loadProducts();
        await loadDashboard();

    } catch (error) {

        console.error(
            "[ADMIN] Save product error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}

async function toggleProduct(
    productId,
    currentActive
) {

    const question =
        currentActive
            ? "Deactivate this product?"
            : "Activate this product?";

    if (!window.confirm(question)) {
        return;
    }

    try {

        await apiFetch(
            `${API}/products/${productId}/toggle`,
            {
                method: "POST"
            }
        );

        showMessage(
            currentActive
                ? "Product deactivated successfully."
                : "Product activated successfully."
        );

        await loadProducts();

    } catch (error) {

        console.error(
            "[ADMIN] Toggle error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}

function renderLowStock() {

    const box =
        $("lowStock");

    if (!box) {
        return;
    }

    const products =
        currentProducts
            .filter(
                (product) =>
                    Number(
                        product.stock ??
                        0
                    ) <= 3
            )
            .sort(
                (a, b) =>
                    Number(
                        a.stock ??
                        0
                    ) -
                    Number(
                        b.stock ??
                        0
                    )
            )
            .slice(
                0,
                8
            );

    if (!products.length) {

        box.innerHTML = `
            <div class="empty">
                No low-stock products
            </div>
        `;

        return;
    }

    box.innerHTML =
        products
            .map(
                (product) => `

                    <div class="list-item">

                        <div>

                            <strong>
                                ${escapeHTML(
                                    product.name ||
                                    "Product"
                                )}
                            </strong>

                            <small>
                                Stock:
                                ${escapeHTML(
                                    product.stock ??
                                    0
                                )}
                            </small>

                        </div>

                        <span>
                            ${escapeHTML(
                                product.stock ??
                                0
                            )}
                        </span>

                    </div>

                `
            )
            .join("");
}

/* =========================================================
   INVENTORY
   ========================================================= */

async function loadInventory() {

    const box =
        $("inventoryTable");

    setLoading(
        "inventoryTable"
    );

    try {

        const data =
            await apiFetch(
                `${API}/inventory`
            );

        currentInventory =
            normalizeList(
                data,
                [
                    "inventory",
                    "items"
                ]
            );

        const productFilter =
            $("inventoryProductFilter")
                ?.value ||
            "";

        const statusFilter =
            $("inventoryStatusFilter")
                ?.value ||
            "";

        let items =
            [...currentInventory];

        if (productFilter) {

            items =
                items.filter(
                    (item) =>
                        String(
                            item.product_id ??
                            ""
                        ) ===
                        String(
                            productFilter
                        )
                );
        }

        if (statusFilter) {

            items =
                items.filter(
                    (item) =>
                        String(
                            item.status ||
                            ""
                        ) ===
                        String(
                            statusFilter
                        )
                );
        }

        if (!items.length) {

            box.innerHTML = `
                <div class="empty">
                    No inventory available
                </div>
            `;

            return;
        }

        box.innerHTML = `
            <table>

                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Product</th>
                        <th>Status</th>
                        <th>Item Data</th>
                        <th>Created</th>
                        <th>Sold</th>
                    </tr>
                </thead>

                <tbody>

                    ${items.map(
                        (item) => `

                        <tr>

                            <td>
                                ${escapeHTML(
                                    item.id
                                )}
                            </td>

                            <td>
                                ${escapeHTML(
                                    item.product_name ||
                                    findProduct(
                                        item.product_id
                                    )?.name ||
                                    "-"
                                )}
                            </td>

                            <td>
                                ${escapeHTML(
                                    item.status ||
                                    "-"
                                )}
                            </td>

                            <td>
                                ${escapeHTML(
                                    item.item_data ||
                                    "-"
                                )}
                            </td>

                            <td>
                                ${escapeHTML(
                                    item.created_at ||
                                    "-"
                                )}
                            </td>

                            <td>
                                ${escapeHTML(
                                    item.sold_at ||
                                    "-"
                                )}
                            </td>

                        </tr>

                    `
                    ).join("")}

                </tbody>

            </table>
        `;

    } catch (error) {

        console.error(
            "[ADMIN] Inventory error:",
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

function populateInventoryProductSelect() {

    const modalSelect =
        $("inventoryProduct");

    const filterSelect =
        $("inventoryProductFilter");

    if (modalSelect) {

        const current =
            modalSelect.value;

        modalSelect.innerHTML = `
            <option value="">
                Select product
            </option>
        `;

        currentProducts.forEach(
            (product) => {

                const option =
                    document.createElement(
                        "option"
                    );

                option.value =
                    product.id;

                option.textContent =
                    product.name ||
                    `Product ${product.id}`;

                modalSelect.appendChild(
                    option
                );
            }
        );

        if (current) {
            modalSelect.value =
                current;
        }
    }

    if (filterSelect) {

        const current =
            filterSelect.value;

        filterSelect.innerHTML = `
            <option value="">
                All products
            </option>
        `;

        currentProducts.forEach(
            (product) => {

                const option =
                    document.createElement(
                        "option"
                    );

                option.value =
                    product.id;

                option.textContent =
                    product.name ||
                    `Product ${product.id}`;

                filterSelect.appendChild(
                    option
                );
            }
        );

        if (current) {
            filterSelect.value =
                current;
        }
    }
}

function openInventoryModal() {

    const form =
        $("inventoryForm");

    if (form) {
        form.reset();
    }

    populateInventoryProductSelect();

    openModal(
        "inventoryModal"
    );
}

async function saveInventory(event) {
    event.preventDefault();

    const productId =
        $("inventoryProduct")?.value || "";

    const rawText =
        $("inventoryItemData")?.value || "";

    // One inventory item per line.
    const items = [
        ...new Set(
            rawText
                .split(/\r?\n/)
                .map(
                    (value) =>
                        value.trim()
                )
                .filter(Boolean)
        )
    ];

    if (!productId) {
        showMessage(
            "Please select a product.",
            "error"
        );

        return;
    }

    if (!items.length) {
        showMessage(
            "Enter at least one inventory item.",
            "error"
        );

        return;
    }

    if (items.length > 500) {
        showMessage(
            "Maximum 500 inventory items per request.",
            "error"
        );

        return;
    }

    try {

        const data =
            await apiFetch(
                `${API}/inventory`,
                {
                    method: "POST",

                    body:
                        JSON.stringify({
                            product_id:
                                Number(
                                    productId
                                ),

                            items
                        })
                }
            );

        const inserted =
            Number(
                data.inserted ?? 0
            );

        const skipped =
            Number(
                data.skipped_existing ?? 0
            );

        const stock =
            data.available_stock ??
            "-";

        let message =
            `${inserted} inventory item(s) added. ` +
            `Available stock: ${stock}.`;

        if (skipped > 0) {

            message +=
                ` ${skipped} duplicate item(s) skipped.`;
        }

        showMessage(
            message
        );

        closeModal(
            "inventoryModal"
        );

        await loadInventory();

        await loadProducts();

        await loadDashboard();

    } catch (error) {

        console.error(
            "[ADMIN] Save inventory error:",
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

    setLoading(
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

    if (!box) {
        return;
    }

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

                    const text = [

                        order.id,

                        order.user_id,

                        order.telegram_id,

                        order.username,

                        order.product_name,

                        order.status

                    ]
                        .join(" ")
                        .toLowerCase();

                    return text.includes(
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
                    String(
                        statusFilter
                    )
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
                </tr>
            </thead>

            <tbody>

                ${orders.map(
                    (order) => `

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

                    </tr>

                `
                ).join("")}

            </tbody>

        </table>
    `;
}

/* =========================================================
   PAYMENTS
   ========================================================= */

async function loadPayments() {

    const box =
        $("paymentsTable");

    setLoading(
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

    if (!box) {
        return;
    }

    const search =
        $("paymentSearch")
            ?.value
            ?.trim()
            .toLowerCase() ||
        "";

    let payments =
        [...currentPayments];

    if (search) {

        payments =
            payments.filter(
                (payment) => {

                    const text = [

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

                    return text.includes(
                        search
                    );
                }
            );
    }

    if (!payments.length) {

        box.innerHTML = `
            <div class="empty">
                No payments available
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
                    <th>Stars</th>
                    <th>Status</th>
                    <th>Telegram Charge</th>
                    <th>Provider Charge</th>
                    <th>Created</th>
                </tr>
            </thead>

            <tbody>

                ${payments.map(
                    (payment) => `

                    <tr>

                        <td>
                            #${escapeHTML(
                                payment.id
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                payment.username ||
                                payment.telegram_id ||
                                payment.user_id ||
                                "-"
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
                            ${escapeHTML(
                                payment.telegram_payment_charge_id ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                payment.provider_payment_charge_id ||
                                "-"
                            )}
                        </td>

                        <td>
                            ${escapeHTML(
                                payment.created_at ||
                                "-"
                            )}
                        </td>

                    </tr>

                `
                ).join("")}

            </tbody>

        </table>
    `;
}

/* =========================================================
   USERS
   ========================================================= */

async function loadUsers() {

    const box =
        $("usersTable");

    setLoading(
        "usersTable"
    );

    try {

        const data =
            await apiFetch(
                `${API}/users`
            );

        currentUsers =
            normalizeList(
                data,
                [
                    "users",
                    "items"
                ]
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
                    ${escapeHTML(
                        error.message
                    )}
                </div>
            `;
        }
    }
}

function renderUsers() {

    const box =
        $("usersTable");

    if (!box) {
        return;
    }

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
            users.filter(
                (user) => {

                    const text = [

                        user.id,

                        user.telegram_id,

                        user.username,

                        user.first_name,

                        user.last_name

                    ]
                        .join(" ")
                        .toLowerCase();

                    return text.includes(
                        search
                    );
                }
            );
    }

    if (!users.length) {

        box.innerHTML = `
            <div class="empty">
                No users available
            </div>
        `;

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

                ${users.map(
                    (user) => `

                    <tr>

                        <td>
                            ${escapeHTML(
                                user.id
                            )}
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
                                user.created_at ||
                                "-"
                            )}
                        </td>

                    </tr>

                `
                ).join("")}

            </tbody>

        </table>
    `;
}

/* =========================================================
   CREATE GAME
   ========================================================= */

async function saveGame(event) {

    event.preventDefault();

    const name =
        $("gameName")
            ?.value
            ?.trim() ||
        "";

    const description =
        $("gameDescription")
            ?.value
            ?.trim() ||
        "";

    if (!name) {

        showMessage(
            "Game name is required.",
            "error"
        );

        return;
    }

    try {

        await apiFetch(
            `${API}/games`,
            {
                method: "POST",

                body:
                    JSON.stringify({

                        name,

                        description

                    })
            }
        );

        showMessage(
            "Game created successfully."
        );

        closeModal(
            "gameModal"
        );

        $("gameForm")
            ?.reset();

        await loadGames();
        await loadDashboard();

    } catch (error) {

        console.error(
            "[ADMIN] Save game error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}

/* =========================================================
   CREATE CATEGORY
   ========================================================= */

function openCategoryModal() {

    const form =
        $("categoryForm");

    if (form) {
        form.reset();
    }

    populateGameSelects();

    openModal(
        "categoryModal"
    );
}

async function saveCategory(event) {

    event.preventDefault();

    const gameId =
        $("categoryGame")
            ?.value ||
        "";

    const name =
        $("categoryName")
            ?.value
            ?.trim() ||
        "";

    const description =
        $("categoryDescription")
            ?.value
            ?.trim() ||
        "";

    if (!gameId) {

        showMessage(
            "Please select a game.",
            "error"
        );

        return;
    }

    if (!name) {

        showMessage(
            "Category name is required.",
            "error"
        );

        return;
    }

    try {

        await apiFetch(
            `${API}/categories`,
            {
                method: "POST",

                body:
                    JSON.stringify({

                        game_id:
                            Number(
                                gameId
                            ),

                        name,

                        description

                    })
            }
        );

        showMessage(
            "Category created successfully."
        );

        closeModal(
            "categoryModal"
        );

        $("categoryForm")
            ?.reset();

        await loadCategories();
        await loadDashboard();

    } catch (error) {

        console.error(
            "[ADMIN] Save category error:",
            error
        );

        showMessage(
            error.message,
            "error"
        );
    }
}

/* =========================================================
   FILTERS
   ========================================================= */

function setupFilters() {

    $("productSearch")
        ?.addEventListener(
            "input",
            renderProducts
        );

    $("productGameFilter")
        ?.addEventListener(
            "change",
            renderProducts
        );

    $("productCategoryFilter")
        ?.addEventListener(
            "change",
            renderProducts
        );

    $("inventoryProductFilter")
        ?.addEventListener(
            "change",
            loadInventory
        );

    $("inventoryStatusFilter")
        ?.addEventListener(
            "change",
            loadInventory
        );

    $("orderSearch")
        ?.addEventListener(
            "input",
            renderOrders
        );

    $("orderStatusFilter")
        ?.addEventListener(
            "change",
            renderOrders
        );

    $("paymentSearch")
        ?.addEventListener(
            "input",
            renderPayments
        );

    $("userSearch")
        ?.addEventListener(
            "input",
            renderUsers
        );

    $("categoryGameFilter")
        ?.addEventListener(
            "change",
            loadCategories
        );
}

/* =========================================================
   MODAL SETUP
   ========================================================= */

function setupModals() {

    document
        .querySelectorAll(".modal")
        .forEach((modal) => {

            modal.addEventListener(
                "click",
                (event) => {

                    if (
                        event.target ===
                        modal
                    ) {
                        closeModal(
                            modal.id
                        );
                    }

                }
            );

        });

    const gameForm =
        $("gameForm");

    if (gameForm) {

        gameForm.addEventListener(
            "submit",
            saveGame
        );
    }

    const categoryForm =
        $("categoryForm");

    if (categoryForm) {

        categoryForm.addEventListener(
            "submit",
            saveCategory
        );
    }

    const productForm =
        $("productForm");

    if (productForm) {

        productForm.addEventListener(
            "submit",
            saveProduct
        );
    }

    const inventoryForm =
        $("inventoryForm");

    if (inventoryForm) {

        inventoryForm.addEventListener(
            "submit",
            saveInventory
        );
    }
}

/* =========================================================
   GLOBAL FUNCTIONS
   Required because current HTML uses onclick=""
   ========================================================= */

window.openModal =
    openModal;

window.closeModal =
    closeModal;

window.backdropClose =
    backdropClose;

window.openProductModal =
    openProductModal;

window.editProduct =
    editProduct;

window.toggleProduct =
    toggleProduct;

window.openCategoryModal =
    openCategoryModal;

window.openInventoryModal =
    openInventoryModal;

window.loadDashboard =
    loadDashboard;

window.loadGames =
    loadGames;

window.loadCategories =
    loadCategories;

window.loadProducts =
    loadProducts;

window.loadInventory =
    loadInventory;

window.loadOrders =
    loadOrders;

window.renderOrders =
    renderOrders;

window.loadPayments =
    loadPayments;

window.renderPayments =
    renderPayments;

window.loadUsers =
    loadUsers;

window.renderUsers =
    renderUsers;

window.renderProducts =
    renderProducts;

window.logout =
    logout;

/* =========================================================
   INITIALIZATION
   ========================================================= */

async function initAdmin() {

    console.log(
        "[ADMIN] Unified admin.js loaded."
    );

    const authenticated =
        await checkAuth();

    if (!authenticated) {

        console.error(
            "[ADMIN] Authentication failed."
        );

        return;
    }

    setupNavigation();

    setupFilters();

    setupModals();

    setLoading(
        "recentOrders"
    );

    setLoading(
        "productsTable"
    );

    setLoading(
        "gamesTable"
    );

    setLoading(
        "categoriesTable"
    );

    const results =
        await Promise.allSettled([
            loadGames(),
            loadCategories(),
            loadProducts(),
            loadDashboard()
        ]);

    console.log(
        "[ADMIN] Initial loading:",
        results.map(
            (result) =>
                result.status
        )
    );

    switchSection(
        "dashboard"
    );

    console.log(
        "[ADMIN] Initialization finished."
    );
}

/*
 * The script is currently loaded at the
 * end of <body>, but this also safely handles
 * a future move into <head>.
 */
if (
    document.readyState ===
    "loading"
) {

    document.addEventListener(
        "DOMContentLoaded",
        initAdmin,
        {
            once: true
        }
    );

} else {

    initAdmin();

           }
