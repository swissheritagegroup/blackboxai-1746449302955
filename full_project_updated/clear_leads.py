from database.db_handler import clear_leads

def main():
    try:
        clear_leads()
        print("Leads database has been cleared successfully.")
    except Exception as e:
        print(f"An error occurred while clearing leads database: {e}")

if __name__ == "__main__":
    main()
