import os
import io
import logging
import openai
import mimetypes
from flask_cors import CORS
from decouple import config
from flask import Flask, request, jsonify, send_file, abort
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.storage.blob import BlobServiceClient
from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from approaches.chat import ChatReadRetrieveReadApproach as ChatGPT4
from retry import retry
from wrapt_timeout_decorator import timeout
from openai import error

# Replace these with your own values, either in environment variables or directly here
# AZURE_STORAGE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT") or "mystorageaccount"
# AZURE_STORAGE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER") or "content"
# AZURE_SEARCH_SERVICE = os.environ.get("AZURE_SEARCH_SERVICE") or "gptkb"
# AZURE_STORAGE_KEY = os.environ.get("AZURE_STORAGE_KEY") or "mykey"
# AZURE_SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX") or "gptkbindex"
# AZURE_SEARCH_SERVICE_KEY = os.environ.get("AZURE_SEARCH_SERVICE_KEY") or "mykey"
# AZURE_OPENAI_SERVICE = os.environ.get("AZURE_OPENAI_SERVICE") or "myopenai"
# AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CHATGPT_DEPLOYMENT") or "chat"
# AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY") or "mykey"
# KB_FIELDS_CONTENT = os.environ.get("KB_FIELDS_CONTENT") or "content"
# KB_FIELDS_CATEGORY = os.environ.get("KB_FIELDS_CATEGORY") or "category"
# KB_FIELDS_SOURCEPAGE = os.environ.get("KB_FIELDS_SOURCEPAGE") or "sourcepage"

AZURE_STORAGE_ACCOUNT = config("AZURE_STORAGE_ACCOUNT") or "mystorageaccount"
AZURE_STORAGE_CONTAINER = config("AZURE_STORAGE_CONTAINER") or "content"
AZURE_STORAGE_KEY = config("AZURE_STORAGE_KEY") or "mykey"
AZURE_SEARCH_SERVICE = config("AZURE_SEARCH_SERVICE") or "gptkb"
AZURE_SEARCH_INDEX = config("AZURE_SEARCH_INDEX") or "gptkbindex"
AZURE_SEARCH_SERVICE_KEY = config("AZURE_SEARCH_SERVICE_KEY") or "mykey"
AZURE_OPENAI_SERVICE = config("AZURE_OPENAI_SERVICE") or "myopenai"
AZURE_OPENAI_CHATGPT_DEPLOYMENT = config("AZURE_OPENAI_CHATGPT_DEPLOYMENT") or "chat"
AZURE_OPENAI_KEY = config("AZURE_OPENAI_KEY") or "mykey"
KB_FIELDS_CONTENT = config("KB_FIELDS_CONTENT") or "content"
KB_FIELDS_CATEGORY = config("KB_FIELDS_CATEGORY") or "category"
KB_FIELDS_SOURCEPAGE = config("KB_FIELDS_SOURCEPAGE") or "sourcepage"

print('AZURE_STORAGE_ACCOUNT= ', AZURE_STORAGE_ACCOUNT)
print('AZURE_STORAGE_CONTAINER= ', AZURE_STORAGE_CONTAINER)
print('AZURE_SEARCH_SERVICE= ', AZURE_SEARCH_SERVICE)
print('AZURE_STORAGE_KEY= ', AZURE_STORAGE_KEY)
print('AZURE_SEARCH_INDEX= ', AZURE_SEARCH_INDEX)
print('AZURE_OPENAI_SERVICE= ', AZURE_OPENAI_SERVICE)
print('AZURE_OPENAI_CHATGPT_DEPLOYMENT= ', AZURE_OPENAI_CHATGPT_DEPLOYMENT)
print('AZURE_OPENAI_KEY= ', AZURE_OPENAI_KEY)
print('AZURE_SEARCH_SERVICE_KEY= ', AZURE_SEARCH_SERVICE_KEY)
print(KB_FIELDS_CONTENT, KB_FIELDS_CATEGORY, KB_FIELDS_SOURCEPAGE)

# Use the current user identity to authenticate with Azure OpenAI, Cognitive Search and Blob Storage (no secrets needed, 
# just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the 
# keys for each service
# If you encounter a blocking error during a DefaultAzureCredntial resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
azure_credential = AZURE_STORAGE_KEY# DefaultAzureCredential()
azure_credential_search = AzureKeyCredential(AZURE_SEARCH_SERVICE_KEY)
# Used by the OpenAI SDK
openai.api_type = "azure"
openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com"
openai.api_version = "2023-05-15"

# Comment these two lines out if using keys, set your API key in the OPENAI_API_KEY environment variable instead
openai.api_type = "azure"
openai.api_key = AZURE_OPENAI_KEY

# Set up clients for Cognitive Search and Storage
search_client = SearchClient(
    endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
    index_name=AZURE_SEARCH_INDEX,
    credential=azure_credential_search)

blob_client = BlobServiceClient(
    account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net", 
    credential=azure_credential)
blob_container = blob_client.get_container_client(AZURE_STORAGE_CONTAINER)

chat_approaches = {
    "rrr": ChatReadRetrieveReadApproach(search_client, AZURE_OPENAI_CHATGPT_DEPLOYMENT, KB_FIELDS_SOURCEPAGE, KB_FIELDS_CONTENT),
    "gpt4": ChatGPT4(search_client, AZURE_OPENAI_CHATGPT_DEPLOYMENT, KB_FIELDS_SOURCEPAGE, KB_FIELDS_CONTENT)
}

app = Flask(__name__)
CORS(app)

@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def static_file(path):
    return app.send_static_file(path)

# Serve content files from blob storage from within the app to keep the example self-contained. 
# *** NOTE *** this assumes that the content files are public, or at least that all users of the app
# can access all the files. This is also slow and memory hungry.
@app.route("/content/<path>")
def content_file(path):
    blob = blob_container.get_blob_client(path).download_blob()
    if not blob.properties or not blob.properties.has_key("content_settings"):
        abort(404)
    mime_type = blob.properties["content_settings"]["content_type"]
    if mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    blob_file = io.BytesIO()
    blob.readinto(blob_file)
    blob_file.seek(0)
    return send_file(blob_file, mimetype=mime_type, as_attachment=False, download_name=path)

@retry((TimeoutError, error.Timeout), logger=logging.getLogger(__name__), delay=2)
@timeout(40)
def run_chat(request: dict, impl):
    print('run_chat' , impl)
    r = impl.run(request["history"], request.get("overrides") or {})
    return r

@app.route("/chat", methods=["POST"])
def chat():
    print('chat')
    if not request.json:
        return jsonify({"error": "request must be json"}), 400
    approach = request.json["approach"]
    try:
        impl = chat_approaches.get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        r = run_chat(request.json, impl)
        return jsonify(r)
    except Exception as e:
        logging.exception("Exception in /chat")
        return jsonify({"error": str(e)}), 500
    
if __name__ == "__main__":
    app.run()