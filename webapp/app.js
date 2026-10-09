const API_BASE = "";

const gameFilter = document.getElementById("gameFilter");
const categoryFilter = document.getElementById("categoryFilter");
const productsContainer = document.getElementById("products");


// =========================================================
// TELEGRAM MINI APP
// =========================================================

function initTelegramWebApp() {
    if (
        window.Telegram &&
        window.Telegram.WebApp
    ) {
        Telegram.WebApp.ready();

        try {
            Telegram.WebApp.expand();
        } catch (error) {
            console.warn(
                "Unable to expand Telegram WebApp:",
                error
            );
        }

        return true;
    }

    return false;
}


// =========================================================
// INITIAL LOAD
// =========================================================

document.addEventListener(
    "DOMContentLoaded",
    async () => {

        initTelegramWebApp();

        await loadGames();
        await loadProducts();
    }
);


// =========================================================
// LOAD GAMES
// =========================================================

async function loadGames() {

    if (!gameFilter) {
        return;
    }

    try {

        const response = await fetch(
            `${API_BASE}/api/games`,
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }

        const data = await response.json();

        if (!data.ok) {
            throw new Error(
                data.error ||
                "Failed to load games."
            );
        }

        gameFilter.innerHTML = `
            <option value="">
                All Games
            </option>
        `;

        const games = data.games || [];

        games.forEach(game => {

            const option =
                document.createElement("option");

            option.value = game.id;
            option.textContent = game.name;

            gameFilter.appendChild(option);
        });

    } catch (error) {

        console.error(
            "Failed to load games:",
            error
        );
    }
}


// =========================================================
// LOAD CATEGORIES
// =========================================================

async function loadCategories(gameId) {

    if (!categoryFilter) {
        return;
    }

    categoryFilter.innerHTML = `
        <option value="">
            All Categories
        </option>
    `;

    categoryFilter.disabled = true;

    if (!gameId) {
        return;
    }

    try {

        const response = await fetch(
            `${API_BASE}/api/games/${gameId}/categories`,
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }

        const data = await response.json();

        if (!data.ok) {
            throw new Error(
                data.error ||
                "Failed to load categories."
            );
        }

        const categories =
            data.categories || [];

        categories.forEach(category => {

            const option =
                document.createElement("option");

            option.value = category.id;
            option.textContent = category.name;

            categoryFilter.appendChild(option);
        });

        categoryFilter.disabled =
            categories.length === 0;

    } catch (error) {

        console.error(
            "Failed to load categories:",
            error
        );

        categoryFilter.disabled = true;
    }
}


// =========================================================
// LOAD PRODUCTS
// =========================================================

async function loadProducts() {

    if (!productsContainer) {
        return;
    }
      productsContainer.innerHTML = `
        <p class="loading">
            Loading products...
        </p>
    `;

    try {

        const params =
            new URLSearchParams();

        const gameId =
            gameFilter?.value || "";

        const categoryId =
            categoryFilter?.value || "";


        if (gameId) {
            params.set(
                "game_id",
                gameId
            );
        }

        if (categoryId) {
            params.set(
                "category_id",
                categoryId
            );
        }


        const query =
            params.toString();

        const url =
            query
                ? `${API_BASE}/api/products?${query}`
                : `${API_BASE}/api/products`;


        const response = await fetch(
            url,
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }


        const data = await response.json();

        if (!data.ok) {
            throw new Error(
                data.error ||
                "Failed to load products."
            );
        }


        const products =
            Array.isArray(data.products)
                ? data.products
                : [];


        if (products.length === 0) {

            productsContainer.innerHTML = `
                <div class="empty-state">

                    <h3>
                        No products found
                    </h3>

                    <p>
                        There are no products
                        matching your selection.
                    </p>

                </div>
            `;

            return;
        }


        productsContainer.innerHTML =
            products.map(product => {

                const productId =
                    Number(product.id);

                const name =
                    escapeHtml(
                        product.name ||
                        "Unnamed Product"
                    );

                const description =
                    escapeHtml(
                        product.description ||
                        ""
                    );

                const gameName =
                    escapeHtml(
                        product.game_name ||
                        "Unknown Game"
                    );

                const categoryName =
                    escapeHtml(
                        product.category_name ||
                        "Unknown Category"
                    );

                const price =
                    Number(
                        product.price_stars || 0
                    );

                const stock =
                    Number(
                        product.stock || 0
                    );

                return `
                    <article
                        class="product-card"
                        onclick="openProduct(${productId})"
                    >

                        <div class="product-content">

                            <div class="product-info">

                                <h3>
                                    ${name}
                                </h3>

                                <p class="product-description">
                                    ${description}
                                </p>

                                <div class="product-details">

                                    <span>
                                        🎮
                                        ${gameName}
                                    </span>

                                    <span>
                                        📂
                                        ${categoryName}
                                    </span>

                                </div>

                            </div>


                            <div class="product-bottom">

                                <strong
                                    class="product-price"
                                >
                                    ⭐ ${price}
                                </strong>

                                <span
                                    class="product-stock"
                                >
                                    Stock: ${stock}
                                </span>

                            </div>

                        </div>

                    </article>
                `;
            }).join("");


    } catch (error) {

        console.error(
            "Failed to load products:",
            error
        );

        productsContainer.innerHTML = `
            <div class="error-state">
                <h3>
                    Unable to load products
                </h3>

                <p>
                    Please try again later.
                </p>

            </div>
        `;
    }
}


// =========================================================
// GAME FILTER
// =========================================================

if (gameFilter) {

    gameFilter.addEventListener(
        "change",
        async () => {

            const gameId =
                gameFilter.value;

            await loadCategories(gameId);

            await loadProducts();
        }
    );
}


// =========================================================
// CATEGORY FILTER
// =========================================================

if (categoryFilter) {

    categoryFilter.addEventListener(
        "change",
        async () => {

            await loadProducts();
        }
    );
}


// =========================================================
// OPEN PRODUCT
// =========================================================

function openProduct(productId) {

    const id =
        Number(productId);

    if (!Number.isInteger(id) || id <= 0) {

        console.error(
            "Invalid product ID:",
            productId
        );

        return;
    }

    window.location.href =
        `/product/${id}`;
}


// =========================================================
// ESCAPE HTML
// =========================================================

function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }
