/* Progressive enhancement for the Sondermark storefront. Everything works without JS. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var currency = document.querySelector("[data-price]") ? (document.querySelector("[data-price]").textContent.trim().replace(/[\d.,\s]/g, "") || "€") : "€";
  var fmt = function (n) { return currency + n.toFixed(2); };

  // Mobile navigation
  var toggle = document.querySelector("[data-nav-toggle]"), nav = document.getElementById("site-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Catalog filter drawer (mobile)
  var filters = document.getElementById("filters");
  document.querySelectorAll("[data-filters-open]").forEach(function (b) { b.addEventListener("click", function () { filters.classList.add("open"); }); });
  document.querySelectorAll("[data-filters-close]").forEach(function (b) { b.addEventListener("click", function () { filters.classList.remove("open"); }); });
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) { el.addEventListener("change", function () { el.form.submit(); }); });

  // Gallery thumbnails
  var main = document.getElementById("main-image");
  document.querySelectorAll(".thumb").forEach(function (thumb) {
    thumb.addEventListener("click", function () {
      if (!main) return;
      main.src = thumb.getAttribute("data-full");
      main.alt = thumb.getAttribute("data-alt") || main.alt;
      document.querySelectorAll(".thumb").forEach(function (t) { t.classList.remove("active"); });
      thumb.classList.add("active");
    });
  });

  // Quantity steppers
  document.querySelectorAll("[data-qty]").forEach(function (box) {
    var input = box.querySelector("input"), form = box.closest("form");
    var min = parseInt(input.min || "1", 10), max = parseInt(input.max || "99", 10);
    var change = function (delta) {
      var value = Math.min(max, Math.max(min, (parseInt(input.value, 10) || min) + delta));
      input.value = value;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      if (form && form.hasAttribute("data-qty-form")) form.submit();
    };
    box.querySelector("[data-qty-minus]").addEventListener("click", function () { change(-1); });
    box.querySelector("[data-qty-plus]").addEventListener("click", function () { change(1); });
    if (form && form.hasAttribute("data-qty-form")) input.addEventListener("change", function () { form.submit(); });
  });

  // Variant picker → price, stock and button total
  var buyForm = document.querySelector("[data-add-to-cart]");
  if (buyForm) {
    var price = document.querySelector("[data-price]"), total = buyForm.querySelector("[data-total]"),
        stockLine = buyForm.querySelector("[data-stock-line]"), qty = buyForm.querySelector("input[name=quantity]");
    var update = function () {
      var chosen = buyForm.querySelector("input[name=variant]:checked") || buyForm.querySelector("input[name=variant][type=hidden]");
      if (!chosen) return;
      var unit = parseFloat(chosen.getAttribute("data-price")), stock = parseInt(chosen.getAttribute("data-stock"), 10),
          quantity = Math.max(1, parseInt(qty.value, 10) || 1);
      if (price) price.textContent = fmt(unit);
      if (total) total.textContent = fmt(unit * quantity);
      qty.max = Math.max(1, Math.min(99, stock));
      if (quantity > stock) { qty.value = stock; if (total) total.textContent = fmt(unit * stock); }
      if (stockLine) {
        var dot = stockLine.querySelector(".dot");
        stockLine.lastChild.textContent = stock <= 0 ? "Sold out in this option" : (stock <= 12 ? "In stock · only " + stock + " left · ships within 48 hours" : "In stock · ships within 48 hours");
        if (dot) dot.className = "dot " + (stock <= 0 ? "out" : stock <= 12 ? "low" : "in");
      }
    };
    buyForm.querySelectorAll("input[name=variant]").forEach(function (r) { r.addEventListener("change", update); });
    qty.addEventListener("change", update);
    qty.addEventListener("input", update);
    update();
  }
  //Mini-cart
     var cartLink = document.querySelector(".cart-link");
     var miniCart = document.getElementById("mini-cart");
     var miniCartBody = document.getElementById("mini-cart__body");
     var miniCartClose = document.querySelector(".mini-cart__close");
     function updateCartBadge(value) {
      var count = Number(value);
      var badge = document.querySelector("[data-cart-count]");
      if (badge) {
        badge.textContent = count;
        badge.hidden = count === 0;
      }
      if (cartLink) {
        cartLink.setAttribute("aria-label", "Cart, " + count + (count === 1 ? " item" : " items"));
      }
     }
      if (cartLink && miniCart) {
        cartLink.setAttribute("aria-expanded", "false");
        cartLink.addEventListener("click",
          function (e) {
            e.preventDefault();
            miniCart.hidden = false;
            cartLink.setAttribute("aria-expanded", "true");
            fetch(cartLink.href)
            .then(function (response) {
              if (!response.ok) {
                throw new Error("Could not load cart");
              }
              return response.text()
            })
            .then(function (html) {
              var page = new DOMParser().parseFromString(html, "text/html");
              var pageTotal = page.querySelector(".totals dd.grand");
              var miniTotal = miniCart.querySelector("[data-cart-total]");
              if (miniTotal) {
                miniTotal.textContent = pageTotal ? pageTotal.textContent.trim().replace(/^[^\d-]+/, "") : "0.00";
              }
            })
            .catch(function (error) {
              console.error(error);
            })
          }
        )
      if (miniCartClose && miniCart) {
        miniCartClose.addEventListener("click",
          function () {
            miniCart.hidden = true;
            cartLink.setAttribute("aria-expanded", "false");
          }
        )
        }
        document.addEventListener("keydown", function (e) {
          if (e.key === "Escape" && ! miniCart.hidden) {
            miniCart.hidden = true;
            cartLink.setAttribute("aria-expanded", "false");
            cartLink.focus();
          }
        });
      }
      var addToCartForms = document.querySelectorAll("[data-add-to-cart]");
      addToCartForms.forEach(function (form) {
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          var errorBox = document.querySelector("[data-mini-cart-error]");
          if (errorBox) {
            errorBox.textContent = "";
            errorBox.hidden = true;
          }
          var formData = new FormData(form);
          var submitButton = form.querySelector('button[type= "submit"]');
          if (submitButton) {
            submitButton.disabled = true;
          }
          var loading = document.querySelector("[data-mini-cart-loading]");
          if (loading) {
            loading.hidden = false;
          }
          if (miniCart && cartLink) {
            miniCart.hidden = false;
          }
          cartLink.setAttribute("aria-expanded", "true");
            fetch(form.action, {
              method: "POST",
              body: formData,
              headers: {"X-Requested-With":"XMLHttpRequest"}
            })
            .then(function (response) {
              return response.json().then(function(data) {
                if (!response.ok) {
                  throw new Error(data.error || "Could not add item to cart");
                }
                return data;
              });
            })
            .then(function (data) {
              if (submitButton) {
                submitButton.disabled = false;
              }
              updateCartBadge(data.cart_count);
              if (miniCart) {
                miniCart.hidden = false;
              }
               var miniCartBody = document.querySelector(".mini-cart__body");
               if (miniCartBody && data.items) {
                miniCartBody.replaceChildren();
                var countText = document.createElement("p");
                countText.textContent = "Items in cart: ";
                var countSpan = document.createElement("span");
                countSpan.setAttribute("data-mini-cart-count", "");
                countSpan.textContent = data.cart_count;
                countText.appendChild(countSpan);
                miniCartBody.appendChild(countText);
                data.items.forEach(function (item) {
                  var itemElement = document.createElement("div");
                  itemElement.className = "mini-cart__item";
                  itemElement.dataset.itemId = item.id;
                  var name = document.createElement("span");
                  name.textContent = item.name;
                  itemElement.appendChild(name);
                  var quantityRow = document.createElement("div");
                  quantityRow.className = "mini-cart__quantity";
                  var minusButton = document.createElement("button");
                  minusButton.type = "button";
                  minusButton.setAttribute("data-mini-minus", "");
                  minusButton.textContent = "-";
                  var quantity = document.createElement("span");
                  quantity.textContent = item.quantity;
                  var plusButton = document.createElement("button");
                  plusButton.type = "button";
                  plusButton.setAttribute("data-mini-plus", "");
                  plusButton.textContent = "+";
                  quantityRow.append(minusButton, quantity, plusButton);
                  itemElement.appendChild(quantityRow);
                  miniCartBody.appendChild(itemElement);
                });
              }
              var subtotal = document.querySelector("[data-cart-subtotal]");
              var total = document.querySelector("[data-cart-total]");
              if (subtotal) {
                subtotal.textContent = data.subtotal;
              }
              if (total) {
                total.textContent = data.total;
              }
            })
            .catch(function (error) {
              var errorBox = document.querySelector("[data-mini-cart-error]");
              if (errorBox) {
                errorBox.textContent = error.message;
                errorBox.hidden = false;
              }
              if (miniCart) {
                miniCart.hidden = false;
              }
              if (submitButton) {
                submitButton.disabled = false;
              }
            })
            .finally(function () {
              if (loading) {
                loading.hidden = true;
              }
            });
        });
      });
      miniCartBody.addEventListener("click", function (e) {
        if (!e.target.closest("[data-mini-plus], [data-mini-minus]")) {
          return;
        }
        var item = e.target.closest(".mini-cart__item");
        if (!item) {
          return;
        }
        var errorBox = document.querySelector("[data-mini-cart-error]");
        if (errorBox) {
          errorBox.textContent = "";
          errorBox.hidden = true;
        }
        var itemId = item.dataset.itemId;
        var quantity = item.querySelector(".mini-cart__quantity span");
        var currentQuantity = parseInt(quantity.textContent, 10);
        var csrfToken = document.querySelector('[name="csrfmiddlewaretoken"]').value
        if (e.target.matches("[data-mini-plus]")) {
          quantity.textContent = currentQuantity + 1;
        }
        if (e.target.matches("[data-mini-minus]") && currentQuantity > 0) {
          quantity.textContent = currentQuantity -1;
        }
        var newQuantity = parseInt(quantity.textContent, 10);
        var loading = document.querySelector("[data-mini-cart-loading]");
        if (loading) {
          loading.hidden =false;
        }
        var formData = new FormData();
        formData.append("item", itemId);
        formData.append("quantity", newQuantity);
        formData.append("csrfmiddlewaretoken", csrfToken);
        var buttons = item.querySelectorAll("button");
        buttons.forEach(function (button) {
          button.disabled = true;
        });
        fetch("/cart/update/", {
          method: "POST",
          body: formData,
          headers: { "X-Requested-With": "XMLHttpRequest"}
        })
        .then(function (response) {
          return response.json().then(function(data) {
            if (!response.ok) {
              throw new Error(data.error || "Could not update cart");
            }
            return data;
          });
        })
        .then(function (data) {
          updateCartBadge(data.cart_count);
          var subtotalEl = document.querySelector("[data-cart-subtotal]");
          var totalEl = document.querySelector("[data-cart-total]");
          if (subtotalEl) {
            subtotalEl.textContent = data.subtotal;
          }
          if (totalEl) {
            totalEl.textContent = data.total;
          }
          var miniCartCount =
          document.querySelector("[data-mini-cart-count]");
          if (miniCartCount) {
            miniCartCount.textContent = data.cart_count;
          }
          if (newQuantity === 0) {
            item.remove();
          }
          if (loading) {
            loading.hidden = true;
          }
        })
        .catch(function (error) {
          quantity.textContent = currentQuantity;
          var errorBox = document.querySelector("[data-mini-cart-error]");
          if (errorBox) {
            errorBox.textContent = error.message;
            errorBox.hidden = false;
          }
        })
        .finally(function () {
          if (loading) {
            loading.hidden =true;
          }
          buttons.forEach(function (button) {
            button.disabled = false;
          });
        })
      });
})();
