## Setup

```bash
git clone git@github.com:kaifronsdal/collaborative-auditor.git
cd collaborative-auditor
uv sync
```

### Build the frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

### Configure API keys

Create a `.env` file in the project root with your model provider keys:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

## Run

```bash
uv run collaborative-auditor
```

### Development (with hot-reload frontend)

In one terminal, start the backend:

```bash
uv run collaborative-auditor
```

In another terminal, start the Vite dev server:

```bash
cd frontend
npm run dev
```
