from ai_handler.openai_client import generate_reply

def main():
    prompt = "Hello, how can you assist me with sales?"
    try:
        response = generate_reply(prompt)
        print("Response from model:")
        print(response)
    except Exception as e:
        print(f"Error during OpenAI API call: {e}")

if __name__ == "__main__":
    main()
