# One-command developer and operator workflows.  `make help` lists targets.
PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION     ?= us-central1
ENV        ?= staging
IMAGE      ?= $(REGION)-docker.pkg.dev/$(PROJECT_ID)/adoption-agent-$(ENV)/agent:$(shell git rev-parse --short=12 HEAD)
TF_DIR     := deployment/terraform

.PHONY: help install lint format security test coverage check run resume render playground \
        docker-build docker-run push deploy-job run-job deploy-agent-engine tf-init tf-plan tf-apply lock

help: ## Show this help
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------- develop
install: ## Install pinned deps + dev tools into the active venv
	pip install -r requirements.lock -e ".[dev]" bandit pre-commit
	pre-commit install

lint: ## Ruff lint + format check
	ruff check adoption_agent tests
	ruff format --check adoption_agent tests

format: ## Auto-fix lint and formatting
	ruff check --fix adoption_agent tests
	ruff format adoption_agent tests

security: ## Static security analysis
	bandit -q -ll -r adoption_agent

test: ## Unit, privacy and offline end-to-end tests
	pytest -q

coverage: ## Tests with coverage gate (80%)
	pytest -q --cov=adoption_agent --cov-report=term-missing --cov-fail-under=80

check: lint security coverage ## Everything CI runs locally

lock: ## Regenerate requirements.lock
	uv pip compile pyproject.toml --universal --python-version 3.11 -o requirements.lock

# ------------------------------------------------------------------- run
run: ## Full research cycle (local)
	python -m adoption_agent.cli run

resume: ## Continue from stored evidence (no web research)
	python -m adoption_agent.cli run --resume

render: ## Re-render the HTML report (no model calls)
	python -m adoption_agent.cli render

playground: ## ADK dev UI with traces at http://localhost:8000
	adk web --port 8000 .

# ------------------------------------------------------------ containers
docker-build: ## Build the container image
	docker build -t adoption-agent:local .

docker-run: ## Run a cycle in the container using local ADC
	docker run --rm -e GOOGLE_CLOUD_PROJECT=$(PROJECT_ID) \
	  -v $(HOME)/.config/gcloud:/home/agent/.config/gcloud:ro adoption-agent:local

push: ## Build and push the image to Artifact Registry
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet
	docker build -t $(IMAGE) . && docker push $(IMAGE)

# ---------------------------------------------------------------- deploy
deploy-job: push ## Roll the Cloud Run Job to the freshly pushed image
	gcloud run jobs update adoption-agent-$(ENV) --region $(REGION) --image $(IMAGE)

run-job: ## Execute the deployed Cloud Run Job now
	gcloud run jobs execute adoption-agent-$(ENV) --region $(REGION)

deploy-agent-engine: ## Alternative: deploy the agent to Vertex AI Agent Engine
	adk deploy agent_engine --project $(PROJECT_ID) --region $(REGION) \
	  --display_name adoption-research-agent --trace_to_cloud adoption_agent

# ----------------------------------------------------------- infrastructure
tf-init: ## Terraform init with remote state in GCS
	cd $(TF_DIR) && terraform init -backend-config="bucket=$(PROJECT_ID)-tfstate" \
	  -backend-config="prefix=adoption-agent/$(ENV)"

tf-plan: ## Terraform plan for ENV
	cd $(TF_DIR) && terraform plan -var="project_id=$(PROJECT_ID)" -var="environment=$(ENV)"

tf-apply: ## Terraform apply for ENV
	cd $(TF_DIR) && terraform apply -var="project_id=$(PROJECT_ID)" -var="environment=$(ENV)"
