import os
import glob
import PyPDF2
from googletrans import Translator, LANGUAGES
from gtts import gTTS
import time


# Create an mp3 folder if it doesn't exist
def create_mp3_folder():
    current_dir = os.getcwd()
    mp3_folder_path = os.path.join(current_dir, 'mp3')
    if not os.path.exists(mp3_folder_path):
        os.makedirs(mp3_folder_path)


# Create a pdf folder if it doesn't exist
def create_pdf_folder():
    current_dir = os.getcwd()
    pdf_folder_path = os.path.join(current_dir, 'pdf')
    if not os.path.exists(pdf_folder_path):
        os.makedirs(pdf_folder_path)


# Get a list of PDF files
def get_pdf_files():
    pdf_files = glob.glob('pdf/*.pdf')

    if len(pdf_files) == 0:
        print('No pdf files to convert.')
        return None

    print('\nList of PDF files:')
    for i, file_path in enumerate(pdf_files):
        file_name = os.path.basename(file_path)
        print(f'{i+1}. {file_name}')

    return pdf_files


# Select PDF file
def get_pdf_file_and_convert(pdf_files):
    while True:
        file_input = input(
            'Enter PDF file number to convert to MP3 or type "end" to exit: '
        ).strip().lower()

        if file_input == "end":
            return None

        try:
            file_number = int(file_input)

            if file_number < 1 or file_number > len(pdf_files):
                print(f'The number must be from 1 to {len(pdf_files)}')
            else:
                return pdf_files[file_number - 1]

        except ValueError:
            print('Enter a valid number or type "end" to exit.')


# Select language
def select_language():
    print("\nAvailable Languages:")
    language_codes = list(LANGUAGES.keys())

    for i, code in enumerate(language_codes, start=1):
        print(f"{i}. {LANGUAGES[code].title()} ({code})")

    print(f"{len(language_codes)+1}. Keep original language")

    while True:
        language_input = input(
            '\nEnter language number or type "end" to exit: '
        ).strip().lower()

        if language_input == "end":
            return "EXIT"

        try:
            language_choice = int(language_input)

            if 1 <= language_choice <= len(language_codes):
                return language_codes[language_choice - 1]

            elif language_choice == len(language_codes) + 1:
                return None

            else:
                print("Invalid choice. Try again.")

        except ValueError:
            print("Enter a valid number or type 'end' to exit.")


def PdfConverter():

    create_pdf_folder()
    create_mp3_folder()

    while True:

        pdf_files = get_pdf_files()
        if pdf_files is None:
            print("End of the program.")
            return

        pdf_file_to_convert = get_pdf_file_and_convert(pdf_files)
        if pdf_file_to_convert is None:
            print("End of the program.")
            return

        language = select_language()
        if language == "EXIT":
            print("End of the program.")
            return

        # Open PDF file
        try:
            with open(pdf_file_to_convert, 'rb') as pdf_file:
                pdf_reader = PyPDF2.PdfReader(pdf_file)
                text = ''

                for page in pdf_reader.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted.strip().replace("\n", " ").replace(" ' ", "'")

        except Exception as e:
            print(f"Error reading PDF: {e}")
            continue

        # Translate text if needed
        if language:
            translator = Translator()
            translation_text = ''

            for i in range(0, len(text), 5000):
                text_chunk = text[i:i+5000]

                if not text_chunk.strip():
                    continue

                try:
                    translation = translator.translate(text_chunk, dest=language)
                    if translation and translation.text:
                        translation_text += translation.text
                    else:
                        translation_text += text_chunk

                except Exception as e:
                    print(f"Error during translation: {e}")
                    time.sleep(1)
                    continue

            text = translation_text

        # Detect language automatically
        elif language is None:
            translator = Translator()
            try:
                detected = translator.detect(text[:5000])
                language = detected.lang
            except Exception:
                language = 'en'

        print("The file is being written. Please wait...")

        # Convert to speech
        try:
            tts = gTTS(text=text, lang=language, slow=False)

            mp3_file_name = (
                os.path.splitext(os.path.basename(pdf_file_to_convert))[0] + '.mp3'
            )

            mp3_file_path = os.path.join('mp3', mp3_file_name)

            tts.save(mp3_file_path)

            print(f'{mp3_file_name} successfully created in mp3 folder.\n')

        except Exception as e:
            print(f"Error during speech synthesis: {e}")
            continue

        # Continue or exit
        while True:
            choice_result = input(
                'Make a choice: 1. Continue, 2. Exit, or type "end": '
            ).strip().lower()

            if choice_result == "1":
                break
            elif choice_result in ["2", "end"]:
                print("End of the program.")
                return
            else:
                print("Invalid input. Try again.")
        continue

if __name__ == '__main__':
    PdfConverter()