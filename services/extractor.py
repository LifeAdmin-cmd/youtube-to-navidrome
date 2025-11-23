import json

from huggingface_hub import hf_hub_download
from llama_cpp import Llama


class MetadataExtractor:
    def __init__(self, model_repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF", model_file="qwen2.5-1.5b-instruct-q8_0.gguf"):
        # Download model automatically to local cache
        model_path = hf_hub_download(repo_id=model_repo, filename=model_file)
        
        # Initialize Llama (n_ctx=2048 is plenty for titles)
        # verbose=False keeps your logs clean
        self.llm = Llama(model_path=model_path, n_ctx=2048, verbose=False)

    def extract_metadata(self, youtube_title: str, channel_name: str) -> dict:
        prompt = f"""<|im_start|>system
You are a music metadata expert. Extract the Artist and Track Title from the input. 
Ignore "Official Video", "Lyrics", "4K", etc. 
If the channel name contains the artist, use it.
Output ONLY valid JSON in this format: {{"artist": "string", "title": "string"}}.<|im_end|>
<|im_start|>user
Input: {youtube_title} (Channel: {channel_name})<|im_end|>
<|im_start|>assistant
"""
        
        output = self.llm(
            prompt, 
            max_tokens=100, 
            stop=["<|im_end|>"], 
            temperature=0.1  # Low temp for deterministic results
        )
        
        try:
            text_res = output["choices"][0]["text"]
            return json.loads(text_res)
        except (json.JSONDecodeError, IndexError):
            # Fallback if LLM fails
            return {"artist": "", "title": youtube_title}