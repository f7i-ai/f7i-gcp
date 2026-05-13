# Artifact Registry repo holding the vertex-trainer training container.
# Built/pushed manually first time; CI takeover is a follow-up.

resource "google_artifact_registry_repository" "vertex_trainer" {
  project       = var.project_id
  location      = var.region
  repository_id = "vertex-trainer"
  description   = "Custom training container for Vertex AI CustomJobs (lstm_vae and friends)."
  format        = "DOCKER"

  labels = local.common_labels

  depends_on = [google_project_service.core_apis]
}

# Vertex CustomJobs run as the vertex-trainer SA, so grant it pull on the repo.
resource "google_artifact_registry_repository_iam_member" "vertex_trainer_pull" {
  project    = var.project_id
  location   = google_artifact_registry_repository.vertex_trainer.location
  repository = google_artifact_registry_repository.vertex_trainer.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.vertex_trainer.email}"
}
