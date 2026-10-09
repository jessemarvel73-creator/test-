const loginForm = document.getElementById("loginForm");
const loginMessage = document.getElementById("loginMessage");

if (loginForm) {
    loginForm.addEventListener("submit", async (event) => {
        event.preventDefault();

        const adminKey = document
            .getElementById("adminKey")
            .value
            .trim();

        loginMessage.textContent = "Logging in...";

        try {
            const response = await fetch("/api/admin/login", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                credentials: "same-origin",
                body: JSON.stringify({
                    admin_key: adminKey,
                }),
            });

            const data = await response.json();

            if (!response.ok || !data.ok) {
                loginMessage.textContent =
                    data.error || "Login failed.";
                return;
            }

            loginMessage.textContent =
                "Login successful. Opening dashboard...";

            setTimeout(() => {
                window.location.href =
                    "/admin/panel";
            }, 500);

        } catch (error) {
            console.error(error);

            loginMessage.textContent =
                "Unable to connect to server.";
        }
    });
}


const addGameForm =
    document.getElementById("addGameForm");

const gameMessage =
    document.getElementById("gameMessage");

const loadGamesButton =
    document.getElementById("loadGamesButton");

const gamesList =
    document.getElementById("gamesList");


if (addGameForm) {

    addGameForm.addEventListener(
        "submit",
        async (event) => {

            event.preventDefault();

            const gameName =
                document
                    .getElementById("gameName")
                    .value
                    .trim();

            const gameDescription =
                document
                    .getElementById("gameDescription")
                    .value
                    .trim();

            if (!gameName) {
                gameMessage.textContent =
                    "Game name is required.";
                return;
            }

            gameMessage.textContent =
                "Adding game...";

            try {

                const response = await fetch(
                    "/api/admin/games",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        credentials: "same-origin",
                        body: JSON.stringify({
                            name: gameName,
                            description:
                                gameDescription,
                        }),
                    }
                );

                const data =
                    await response.json();

                if (!response.ok || !data.ok) {

                    gameMessage.textContent =
                        data.error ||
                        "Failed to add game.";

                    return;
                }

                gameMessage.textContent =
                    "Game added successfully.";

                document
                    .getElementById("gameName")
                    .value = "";

                document
                    .getElementById("gameDescription")
                    .value = "";

                await loadGames();

            } catch (error) {

                console.error(error);

                gameMessage.textContent =
                    "Unable to connect to server.";
            }
        }
    );
}


if (loadGamesButton) {

    loadGamesButton.addEventListener(
        "click",
        loadGames
    );
}


async function loadGames() {

    if (!gamesList) {
        return;
    }

    gamesList.textContent =
        "Loading games...";

    try {

        const response = await fetch(
            "/api/games",
            {
                credentials: "same-origin",
            }
        );

        const data =
            await response.json();

        if (!response.ok || !data.ok) {

            gamesList.textContent =
                data.error ||
                "Failed to load games.";

            return;
        }

        if (!Array.isArray(data.games) ||
            data.games.length === 0) {

            gamesList.textContent =
                "No games added yet.";

            return;
        }

        gamesList.innerHTML =
            data.games.map(game => `
                <div class="game-item">

                    <strong>
                        ${escapeHtml(game.name)}
                    </strong>

                    <p>
                        ${escapeHtml(
                            game.description || ""
                        )}
                    </p>

                </div>
            `).join("");

    } catch (error) {

        console.error(error);

        gamesList.textContent =
            "Unable to load games.";
    }
}


function escapeHtml(value) {

    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}
const addProductForm =
    document.getElementById("addProductForm");

const productMessage =
    document.getElementById("productMessage");

const productGame =
    document.getElementById("productGame");

const productCategory =
    document.getElementById("productCategory");
async function loadProductGames() {

    if (!productGame) {
        return;
    }

    try {

        const response = await fetch(
            "/api/games",
            {
                credentials: "same-origin",
            }
        );

        const data =
            await response.json();

        if (!response.ok || !data.ok) {

            console.error(
                data.error ||
                "Failed to load games."
            );

            return;
        }

        productGame.innerHTML = `
            <option value="">
                Select Game
            </option>
        `;

        data.games.forEach(game => {

            const option =
                document.createElement("option");

            option.value = game.id;
            option.textContent = game.name;

            productGame.appendChild(option);
        });

    } catch (error) {

        console.error(
            "Failed to load product games:",
            error
        );
    }
}

const loadProductsButton =
    document.getElementById("loadProductsButton");

const adminProductsList =
    document.getElementById("adminProductsList");


if (addProductForm) {

    addProductForm.addEventListener(
        "submit",
        async (event) => {

            event.preventDefault();

            const name =
                document
                    .getElementById("productName")
                    .value
                    .trim();

            const description =
                document
                    .getElementById("productDescription")
                    .value
                    .trim();

            const gameId =
                Number(productGame.value);

            const categoryId =
                Number(productCategory.value);

            const priceStars =
                Number(
                    document
                        .getElementById("productPrice")
                        .value
                );

            const stock =
                Number(
                    document
                        .getElementById("productStock")
                        .value
                );

            const featured =
                document
                    .getElementById("productFeatured")
                    .checked;


            if (!name) {
                productMessage.textContent =
                    "Product name is required.";
                return;
            }

            if (!gameId) {
                productMessage.textContent =
                    "Please select a game.";
                return;
            }

            if (!categoryId) {
                productMessage.textContent =
                    "Please select a category.";
                return;
            }

            if (priceStars < 0 || stock < 0) {
                productMessage.textContent =
                    "Price and stock cannot be negative.";
                return;
            }


            productMessage.textContent =
                "Adding product...";


            try {

                const response = await fetch(
                    "/api/admin/products",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json",
                        },

                        credentials: "same-origin",

                        body: JSON.stringify({
                            name: name,
                            description: description,
                            game_id: gameId,
                            category_id: categoryId,
                            price_stars: priceStars,
                            stock: stock,
                            featured: featured,
                        }),
                    }
                );


                const data =
                    await response.json();


                if (!response.ok || !data.ok) {

                    productMessage.textContent =
                        data.error ||
                        "Failed to add product.";

                    return;
                }


                productMessage.textContent =
                    "Product added successfully.";


                document
                    .getElementById("productName")
                    .value = "";

                document
                    .getElementById("productDescription")
                    .value = "";

                document
                    .getElementById("productPrice")
                    .value = "";

                document
                    .getElementById("productStock")
                    .value = "1";

                document
                    .getElementById("productFeatured")
                    .checked = false;


                await loadProducts();


            } catch (error) {

                console.error(error);

                productMessage.textContent =
                    "Unable to connect to server.";
            }
        }
    );
}


if (loadProductsButton) {

    loadProductsButton.addEventListener(
        "click",
        loadProducts
    );
}


async function loadProducts() {

    if (!adminProductsList) {
        return;
    }

    adminProductsList.textContent =
        "Loading products...";


    try {

        const response =
            await fetch(
                "/api/products",
                {
                    credentials: "same-origin",
                }
            );


        const data =
            await response.json();


        if (!response.ok || !data.ok) {

            adminProductsList.textContent =
                data.error ||
                "Failed to load products.";

            return;
        }


        if (
            !Array.isArray(data.products) ||
            data.products.length === 0
        ) {

            adminProductsList.textContent =
                "No products added yet.";

            return;
        }


        adminProductsList.innerHTML =
            data.products.map(product => `
                <div class="product-item">

                    <strong>
                        ${escapeHtml(
                            product.name || ""
                        )}
                    </strong>

                    <p>
                        ${escapeHtml(
                            product.description || ""
                        )}
                    </p>

                    <p>
                        Game:
                        ${escapeHtml(
                            product.game_name || "Unknown"
                        )}
                    </p>

                    <p>
                        Category:
                        ${escapeHtml(
                            product.category_name || "Unknown"
                        )}
                    </p>

                    <p>
                        ⭐ ${product.price_stars ?? 0}
                        |
                        Stock: ${product.stock ?? 0}
                    </p>

                </div>
            `).join("");


    } catch (error) {

        console.error(error);

        adminProductsList.textContent =
            "Unable to load products.";
    }
}
const addCategoryForm =
    document.getElementById("addCategoryForm");

const categoryMessage =
    document.getElementById("categoryMessage");

const categoryGame =
    document.getElementById("categoryGame");

const loadCategoriesButton =
    document.getElementById("loadCategoriesButton");

const categoriesList =
    document.getElementById("categoriesList");


async function loadCategoryGames() {

    if (!categoryGame) {
        return;
    }

    try {

        const response = await fetch(
            "/api/games",
            {
                credentials: "same-origin",
            }
        );

        const data = await response.json();

        if (!response.ok || !data.ok) {
            return;
        }

        categoryGame.innerHTML = `
            <option value="">
                Select Game
            </option>
        `;

        data.games.forEach(game => {

            const option =
                document.createElement("option");

            option.value = game.id;
            option.textContent = game.name;

            categoryGame.appendChild(option);
        });

    } catch (error) {

        console.error(
            "Failed to load games:",
            error
        );
    }
}


if (addCategoryForm) {

    addCategoryForm.addEventListener(
        "submit",
        async (event) => {

            event.preventDefault();

            const gameId =
                Number(categoryGame.value);

            const name =
                document
                    .getElementById("categoryName")
                    .value
                    .trim();

            const description =
                document
                    .getElementById("categoryDescription")
                    .value
                    .trim();


            if (!gameId) {

                categoryMessage.textContent =
                    "Please select a game.";

                return;
            }


            if (!name) {

                categoryMessage.textContent =
                    "Category name is required.";

                return;
            }


            categoryMessage.textContent =
                "Adding category...";


            try {

                const response = await fetch(
                    "/api/admin/categories",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json",
                        },

                        credentials: "same-origin",

                        body: JSON.stringify({
                            game_id: gameId,
                            name: name,
                            description:
                                description,
                        }),
                    }
                );


                const data =
                    await response.json();


                if (!response.ok || !data.ok) {

                    categoryMessage.textContent =
                        data.error ||
                        "Failed to add category.";

                    return;
                }


                categoryMessage.textContent =
                    "Category added successfully.";


                document
                    .getElementById("categoryName")
                    .value = "";

                document
                    .getElementById("categoryDescription")
                    .value = "";


                await loadCategories();

            } catch (error) {

                console.error(error);

                categoryMessage.textContent =
                    "Unable to connect to server.";
            }
        }
    );
}


if (loadCategoriesButton) {

    loadCategoriesButton.addEventListener(
        "click",
        loadCategories
    );
}


async function loadCategories() {

    if (!categoriesList) {
        return;
    }

    const gameId =
        Number(categoryGame.value);


    if (!gameId) {

        categoriesList.textContent =
            "Select a game first.";

        return;
    }


    categoriesList.textContent =
        "Loading categories...";


    try {

        const response = await fetch(
            `/api/games/${gameId}/categories`,
            {
                credentials: "same-origin",
            }
        );


        const data =
            await response.json();


        if (!response.ok || !data.ok) {

            categoriesList.textContent =
                data.error ||
                "Failed to load categories.";

            return;
        }


        if (
            !Array.isArray(data.categories) ||
            data.categories.length === 0
        ) {

            categoriesList.textContent =
                "No categories added yet.";

            return;
        }


        categoriesList.innerHTML =
            data.categories.map(category => `
                <div class="category-item">

                    <strong>
                        ${escapeHtml(
                            category.name || ""
                        )}
                    </strong>

                    <p>
                        ${escapeHtml(
                            category.description || ""
                        )}
                    </p>

                </div>
            `).join("");


    } catch (error) {

        console.error(error);

        categoriesList.textContent =
            "Unable to load categories.";
    }
}


if (categoryGame) {

    categoryGame.addEventListener(
        "change",
        loadCategories
    );

    loadCategoryGames();
}
if (productGame) {

    productGame.addEventListener(
        "change",
        async () => {

            const gameId =
                Number(productGame.value);

            productCategory.innerHTML = `
                <option value="">
                    Select Category
                </option>
            `;

            if (!gameId) {
                return;
            }

            try {

                const response =
                    await fetch(
                        `/api/games/${gameId}/categories`,
                        {
                            credentials:
                                "same-origin",
                        }
                    );

                const data =
                    await response.json();

                if (
                    !response.ok ||
                    !data.ok
                ) {

                    console.error(
                        data.error ||
                        "Failed to load categories."
                    );

                    return;
                }

                data.categories.forEach(
                    category => {

                        const option =
                            document.createElement(
                                "option"
                            );

                        option.value =
                            category.id;

                        option.textContent =
                            category.name;

                        productCategory.appendChild(
                            option
                        );
                    }
                );

            } catch (error) {

                console.error(
                    "Failed to load product categories:",
                    error
                );
            }
        }
    );

    loadProductGames();
}

async function loadStoreStatusCard(){
  const btn=document.getElementById("storeStatusToggle"), txt=document.getElementById("storeStatusText"); if(!btn||!txt)return;
  try{const d=await apiFetch("/api/admin/store-status"); const online=!!d.online; btn.textContent=online?"🟢 ONLINE":"🔴 OFFLINE"; btn.classList.toggle("danger",online); btn.classList.toggle("success",!online); txt.textContent=online?"Customers can use the shop and bot.":"Store is in maintenance mode. Admin access remains available."; btn.onclick=async()=>{try{const r=await apiFetch("/api/admin/store-status",{method:"POST",body:JSON.stringify({online:!online})});toast?.(r.online?"Store is online.":"Store is offline.");await loadStoreStatusCard();}catch(e){alert(e.message)}};}catch(e){btn.textContent="Error";txt.textContent=e.message;}
}
window.loadStoreStatusCard=loadStoreStatusCard;
