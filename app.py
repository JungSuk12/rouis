import openai
import requests
from flask import Flask, request
import re
import json
import os
from datetime import datetime

# Initialize the GPT2 tokenizer

# Initialize app settings
app = Flask(__name__)

# Initialize OpenAI API key
openai.api_key = ""  # Your OpenAI API Key

# Define bot characteristics
Name = ""  # Name of the Chatbot
Summary = ""  # Summary of the Chatbot
Personality = ""  # Personality of the Chatbot
Scenario = ""  # Scenario to add on(Chatbot's context)
how_to_reply = "" # Instructions to answer

past = f'''
Name: {Name}
Summary: {Summary}
Personality: {Personality}
Scenario: {Scenario}
'''

# Define path for conversations
conversations_file = "conversations.json"
overflow_conversations_file = "overflow_conversations.json"

# Schfool Parsing Url
url = 'https://school-api.xyz/api/high/J100005170?year=2023&month=' + str(datetime.now().date().month)

def count_tokens(text):
    # This function counts the tokens in a text using the GPT2 tokenizer.
    return len(tokenizer.encode(text, truncation=True))

def load_conversations():
    # This function loads the conversation from a json file.
    # If the file doesn't exist, it returns an empty dictionary.
    if os.path.exists(conversations_file):
        with open(conversations_file, 'r') as file:
            # Read all lines at once, parse each line as JSON, and combine into one dictionary.
            return {k: v for dic in (json.loads(line) for line in file) for k, v in dic.items()}
    else:
        return {}

def save_conversations(conversations):
    # This function saves the conversations into a json file.
    with open(conversations_file, 'w') as file:
        # Save all conversations as one dictionary and serialize as JSON, then write the entire content to file.
        file.write(json.dumps(conversations))

def save_overflow_conversations(user_id, conversation):
    # This function saves the overflow conversations into a json file.
    with open(overflow_conversations_file, 'a') as file:
        file.write(json.dumps({user_id: conversation}))
        file.write('\n')
        
@app.route('/chatbot', methods=['POST'])
def chat():
    # Get the request body and parse it as JSON
    body = request.get_json()

    # Extract user utterance and ID from the request body
    utterance = body['userRequest']['utterance']
    user_id = body['userRequest']['user']['id']

    # Load previous conversations
    conversations = load_conversations()

    # Check if there's a previous conversation with the user
    if user_id not in conversations:
        # If not, initialize a new conversation
        conversations[user_id] = []
    
    # Append user's utterance to the conversation
    conversations[user_id].append({"utterance": utterance})

    # Initialize the messages with the system message
    messages = [
        {"role": "system", "content": past}
    ]

    # Save the updated conversations
    save_conversations(conversations)

    # Add previous utterances and answers to the messages
    for conv in conversations[user_id]:
        if 'answer' not in conv:
            content = conv['utterance']
            messages.append({"role": "user", "content": content})
        else:
            content1 = conv['answer']
            messages.append({"role": "assistant", "content": content1})
    
    # Add user's current utterance to the messages with some instructions in Korean
    messages.append({"role": "user", "content": utterance + how_to_reply})

    # Add user's utterance to messages again
    messages.append({"role" : "user", "content" : utterance})

    # Calculate total token count in the messages
    total_tokens = sum(count_tokens(message["content"]) for message in messages)

    # If total tokens exceed 3500, remove the oldest two messages until it is within limit
    while total_tokens > 3500:
        total_tokens -= count_tokens(messages.pop(0)["content"])
        total_tokens -= count_tokens(messages.pop(0)["content"])

    # Generate the chat completion using the OpenAI API
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        max_tokens=30,
        temperature=1
    )

    # Extract the first sentence from the chat completion as the answer, and remove single quotes
    answer = completion['choices'][0]['message']['content']
    answer = answer.replace("'", "")

    # Form the response body with the answer and quick replies
    responseBody = {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": answer
                    }
                }
            ],
            "quickReplies": [
                {
                    "messageText": utterance,
                    "action": "message",
                    "label": "Reload"
                }
            ]
        }
    }

    # Save the answer to the conversation
    conversations[user_id].append({"answer": answer})
    save_conversations(conversations)

    return responseBody


@app.route('/food', methods=['POST'])
def schfool():
    # Get the current day
    current_date = datetime.now().day

    # Get the request body and parse it as JSON
    body = request.get_json()
    utterance = body['userRequest']['utterance']

    # Get the school menu data from a URL
    response = requests.get(url)
    school_menu = json.loads(response.text)['menu'][current_date - 1]

    def generate_items(meal_type):
        # Function to generate items for each meal type
        return [
            {
                "title": school_menu[meal_type][i].split("(용인)")[0],
                "description": school_menu[meal_type][i].split("(용인)")[1]
            } for i in range(5)
        ]

    def generate_dessert_button(meal_type):
        # Function to generate dessert button for each meal type
        return {
            "label": "Desert",
            "action": "message",
            "messageText": school_menu[meal_type][-1].split("(용인)")[0]
        }
    
    # Prepare the response body
    responseBody = {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "carousel": {
                        "type": "listCard",
                        "items": [
                            {
                                "header": {
                                    "title": meal_type
                                },
                                "items": generate_items(meal_type),
                                "buttons": [generate_dessert_button(meal_type)]
                            } for meal_type in ['breakfast', 'lunch', 'dinner']
                        ]
                    }
                }
            ]
        }
    }

    return responseBody

    
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)