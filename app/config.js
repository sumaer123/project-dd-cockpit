// Local runtime config. The cloud build (cloud/build_cloud.py) OVERWRITES the cloud
// copy of this file with DD_CLOUD=true so the Databricks App hides the local-only
// "Refresh now" button (there is no :8788 helper in the cloud).
window.DD_CLOUD=false;
// Optional: URL of a separate account deep-dive server to embed on account pages.
// Leave empty to hide the Deep Dive tab.
window.DD_DEEPDIVE_URL='';
