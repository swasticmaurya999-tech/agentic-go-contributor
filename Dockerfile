# Bundles the toolchain (Go + Python + git) so the agent runs on any machine with only
# Docker installed. Everything installs INSIDE the image — nothing is installed on your host.
#
# Build:  docker build -t go-contributor .
# Run:    docker run --rm -e GROQ_API_KEY=<your-key> -v "${PWD}/out:/app/out" \
#             go-contributor run --issue https://github.com/spf13/cobra/issues/1816
FROM golang:1.24-bookworm

# Only Python 3 + pip are added (git and CA certs already ship in the golang image).
# No other/unwanted packages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY . .

# Secrets/config arrive at runtime via -e: LLM_PROVIDER, LLM_MODEL, and the matching key
# (GROQ_API_KEY / GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY). Mount
# -v "${PWD}/out:/app/out" to get patch.diff / pr.md / run.log back on the host.
ENTRYPOINT ["python3", "-m", "agent"]
CMD ["--help"]
