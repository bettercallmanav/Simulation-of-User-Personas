# Honda Personas App Flowchart

This Mermaid diagram illustrates the main user flow and processing logic of the Streamlit app for simulating Honda market research interviews with Indian user personas.

```mermaid
flowchart TD
    A[App Launch] --> B[Load Internal Dataset<br/>honda_data_sources.json]
    B --> C[Load Personas<br/>from personas.py]
    C --> D[Check Anthropic API Key]
    D -->|Missing| E[Error: Set API Key<br/>Stop]
    D -->|Valid| F{Persona Selected?}

    F -->|No| G[Show Persona Selection UI<br/>Grid of Cards with Images/Summaries]
    G --> H[User Selects Persona]
    H --> F

    F -->|Yes| I[Show Chat UI<br/>Persona Info/Image in Header & Sidebar]
    I --> J[Sidebar: Toggle Tools<br/>Web Search & Web Fetch]
    J --> K[Display Chat History<br/>Suggested Prompts if First]
    K --> L[User Inputs Query<br/>or Quick Prompt]
    L --> M[Build System Prompt<br/>Base + Persona Roleplay + Guardrails]
    M --> N[Build User Content<br/>Datetime + Relevant Dataset Excerpts + Query]
    N --> O[Optional: Enable Tools<br/>web_search max=10, web_fetch with Citations]
    O --> P[Call Anthropic API<br/>Claude-sonnet-4-5, Stream Response]
    P --> Q[Process Response Blocks<br/>Format Text + Citations + Tool Summary]
    Q --> R[Display Assistant Reply in Chat]
    R --> S[Update Sidebar Sources<br/>Search Hits & Fetched Docs]
    S --> T{Clear or Back?}
    T -->|Clear| U[Reset Session State<br/>Rerun]
    T -->|Back| V[Return to Selection]
    T -->|No| K

    U --> F
    V --> F

    style A fill:#e1f5fe
    style E fill:#ffebee
    style P fill:#f3e5f5
    style R fill:#e8f5e8
```

## Key Notes

- **Initialization**: Caches dataset; stops if no API key.
- **Persona Selection**: Displays 9 personas (e.g., Priya Sharma, Rajesh Kumar) in a 3-column grid.
- **Chat Loop**: AI role-plays in first-person, integrates internal data (top 4 relevant rows via keyword matching), and uses tools for live info.
- **Tools**: Optional; sidebar shows usage summaries (e.g., search queries, fetched URLs).
- **Error Handling**: API errors (rate limits, status) shown in chat; fallback for empty responses.

To view: Open in VS Code (with Mermaid extension), GitHub, or [Mermaid Live](https://mermaid.live).
