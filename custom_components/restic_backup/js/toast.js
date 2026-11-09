// Shows this integration's errors as a toast. Home Assistant has no backend API for
// toasts, and its frontend ignores errors returned for e.g. a delete or a download.
// The backend fires this event with the text to show in "message".
const EVENT = "restic_backup_toast";
// A plain symbol, because the frontend's toast can only display text.
const ERROR_ICON = "❌";

const showToast = (event) => {
  document.querySelector("home-assistant")?.dispatchEvent(
    new CustomEvent("hass-notification", {
      detail: { message: `${ERROR_ICON} ${event.data.message}` },
      bubbles: true,
      composed: true,
    })
  );
};

const { conn } = await window.hassConnection;
// Only admins can subscribe to this event, and only admins can manage backups.
conn.subscribeEvents(showToast, EVENT).catch(() => {});
