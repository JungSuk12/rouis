# Chatbot and School Meal Parser

This project is a Chatbot implemented using Flask and the OpenAI API. It uses GPT-3.5-turbo to generate chat completions. The bot also includes a school meal parser that fetches school menu data from a URL.

## Getting Started

These instructions will get you a copy of the project up and running on your local machine for development and testing purposes.

### Prerequisites

What things you need to install the software:

- Python 3.8 or newer
- pip

### Installing

A step by step series of examples that tell you how to get a development environment running:

1. Clone the repository to your local machine.
2. Navigate to the project directory in the terminal.
3. Install the required packages using pip:
    ```
    pip install -r requirements.txt
    ```

## Running the server

To start the server, use the following command:

python app.py


The server will start running at `http://0.0.0.0:80`.

## Usage

This server has two endpoints, `/chatbot` and `/food`. 

- The `/chatbot` endpoint is a POST method that expects a JSON body containing the user's utterance and ID. It responds with the chatbot's answer and a quick reply.
- The `/food` endpoint is a POST method that expects a JSON body containing the user's utterance. It responds with a carousel of the school's menu for the current day.