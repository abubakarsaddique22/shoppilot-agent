// Login page. Sirf form handle karta hai: login call, session save, redirect.
import { getSession, login, saveSession } from "./api.js";
import { clear, errorText, notice } from "./dom.js";

// Pehle se login hai to seedha console par jao.
if (getSession()) {
  window.location.replace("/index.html");
}

const form = document.getElementById("login-form");
const button = document.getElementById("login-button");
const message = document.getElementById("login-message");

function showError(text) {
  clear(message).append(notice("error", text)); // role="alert", screen reader bhi parhta hai
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clear(message);

  const email = form.elements.email.value.trim();
  const password = form.elements.password.value;
  if (!email || !password) {
    showError("Enter your email and password.");
    return;
  }

  button.disabled = true; // double click se do requests na jayen
  button.textContent = "Logging in...";
  try {
    saveSession(await login(email, password));
    window.location.replace("/index.html");
  } catch (err) {
    showError(errorText(err));
    form.elements.password.value = "";
    form.elements.password.focus();
    button.disabled = false;
    button.textContent = "Log in";
  }
});
