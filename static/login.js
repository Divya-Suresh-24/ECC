(function () {
  const form = document.getElementById("login-form");
  const err = document.getElementById("login-error");
  const btn = document.getElementById("login-btn");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    btn.disabled = true;
    try {
      const res = await fetch("api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "ecc" },
        body: JSON.stringify({ username: form.username.value.trim(), password: form.password.value }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Sign-in failed.");
      window.location.href = document.baseURI;
    } catch (ex) {
      err.textContent = ex.message;
      btn.disabled = false;
    }
  });
})();
