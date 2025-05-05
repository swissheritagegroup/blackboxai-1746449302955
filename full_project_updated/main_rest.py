            else:
                print(f"Failed to send reply to {from_email} for thread {thread_id}")

        print("Sleeping for 60 seconds before next check...")
        time.sleep(60)

if __name__ == "__main__":
    # Set OPENAI_API_KEY environment variable from config
    os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
    asyncio.run(main())
