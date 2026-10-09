const tg = window.Telegram?.WebApp;

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function getProductId() {
    const parts = window.location.pathname.split("/");
    const value = parts[parts.length - 1];

    const id = Number.parseInt(value, 10);

    if (!Number.isInteger(id) || id <= 0) {
        return null;
    }

    return id;
}

async function loadProduct() {
    const container = document.getElementById("productDetails");
    const productId = getProductId();

    if (!productId) {
        container.innerHTML = `
            <div class="error">
                Invalid product ID.
            </div>
        `;
        return;
    }

    try {
        const response = await fetch(
            `/api/products/${productId}?t=${Date.now()}`,
            {
                method: "GET",
                cache: "no-store",
                headers: {
                    "Accept": "application/json"
                }
            }
        );

        const data = await response.json();

        if (!response.ok || !data.ok || !data.product) {
            throw new Error(
                data.error || "Product not found."
            );
        }

        renderProduct(data.product);

    } catch (error) {
        console.error("Product loading failed:", error);

        container.innerHTML = `
            <div class="error">
                ❌ ${escapeHtml(error.message)}
            </div>
        `;
    }
}

function renderProduct(product) {
    const container = document.getElementById("productDetails");

    const images = Array.isArray(product.images)
        ? product.images
        : [];

    const imageHtml = images.length
        ? `
            <div class="product-images">
                ${images.map((item) => `
                    <img
                        src="${escapeHtml(item.image)}"
                        alt="${escapeHtml(product.name)}"
                        loading="lazy"
                    >
                `).join("")}
            </div>
        `
        : "";

    const stock = Number(product.stock ?? 0);
    const price = Number(product.price_stars ?? 0);

    container.innerHTML = `
        <article class="product-detail-card">
            ${imageHtml}

            <div class="product-detail-content">
                <h1>${escapeHtml(product.name)}</h1>

                ${
                    product.game_name
                        ? `<p class="product-meta">🎮 ${escapeHtml(product.game_name)}</p>`
                        : ""
                }

                ${
                    product.category_name
                        ? `<p class="product-meta">📂 ${escapeHtml(product.category_name)}</p>`
                        : ""
                }

                <p class="product-description">
                    ${escapeHtml(product.description || "No description available.")}
                </p>

                <div class="product-purchase-row">
                    <strong>⭐ ${price} Stars</strong>
                    <span>
                                            ${
                            stock > 0
                                ? `Stock: ${stock}`
                                : "Out of stock"
                        }
                    </span>
                </div>

                <button
                    type="button"
                    class="buy-button"
                    onclick="startPurchase(${product.id})"
                    ${stock <= 0 || price <= 0 ? "disabled" : ""}
                >
                    ${
                        stock > 0 && price > 0
                            ? "⭐ Buy Now"
                            : "Out of Stock"
                    }
                </button>
            </div>
        </article>
    `;
}

async function startPurchase(productId) {
    if (!tg) {
        alert("Please open the shop inside Telegram.");
        return;
    }

    if (!tg.initData) {
        alert("Telegram authentication data is unavailable.");
        return;
    }

    try {
        tg.ready();

        const response = await fetch(
            `/api/purchase/${productId}`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                body: JSON.stringify({
                    init_data: tg.initData
                })
            }
        );

        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(
                data.error || "Unable to create invoice."
            );
        }

        if (!data.invoice_url) {
            throw new Error("Telegram invoice URL was not returned.");
        }

        tg.openInvoice(
            data.invoice_url,
            (status) => {
                console.log(
                    "Telegram invoice status:",
                    status
                );

                if (status === "paid") {
                    alert(
                        "✅ Payment successful! Your order is being processed."
                    );
                    loadProduct();
                } else if (status === "cancelled") {
                    console.log("Invoice cancelled by user.");
                } else if (status === "failed") {
                    alert(
                        "❌ Payment failed. Please try again."
                    );
                }
            }
        );

    } catch (error) {
        console.error("Purchase failed:", error);

        alert(
            `❌ ${error.message}`
        );
    }
}

document.addEventListener(
    "DOMContentLoaded",
    () => {
        if (tg) {
            tg.ready();
            tg.expand();
        }

        loadProduct();
    }
);
