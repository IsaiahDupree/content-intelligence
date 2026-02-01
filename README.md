# AI Services

AI/ML microservice for MediaPoster ecosystem - content analysis, generation, and recommendations.

## Services

- **Core** - AI client, LLM API wrapper
- **Content** - Content generation, title generation, recommendations
- **Analysis** - Awareness classification, FATE scoring, sentiment
- **Vision** - Vision API, frame analysis
- **Providers** - OpenAI, Anthropic, and other provider adapters

## Structure

```
services/
├── core/           # AI client wrapper
├── content/        # Content generation
├── analysis/       # Classifiers, scorers
├── vision/         # Vision analysis
└── ai_providers/   # Provider adapters
```

## Port

Default: `:6006`

## Related Repos

- MediaPoster (Core) - Scheduling, publishing
- Remotion - Video rendering
- MediaServices - Media analysis
