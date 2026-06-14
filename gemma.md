from openai import OpenAI
client = OpenAI(
  base_url="https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1",
  api_key="$AI_PLATFORM_API_KEY"
)

stream = client.chat.completions.create(
  model="google/gemma-4-31b-it",
  messages=[
    {
      "role": "assistant",
      "content": "You are an AI assistant tasked with providing information to users."
    },
    {
      "role": "user",
      "content": "What is AI?"
    }
  ],
  max_tokens=2000,
  temperature=1,
  top_p=0.95,
  presence_penalty=0
)
for chunk in stream:
  content = chunk.choices[0].delta.content
  if content:
    print(content)