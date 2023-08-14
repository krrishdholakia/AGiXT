import logging
import os
import asyncio
import sys
import chromadb
from chromadb.config import Settings
from chromadb.api.types import QueryResult
from numpy import array, linalg, ndarray
from hashlib import sha256
from Embedding import Embedding
from datetime import datetime
from collections import Counter
from typing import List
import spacy
from agixtsdk import AGiXTSDK
from dotenv import load_dotenv

load_dotenv()
ApiClient = AGiXTSDK(
    base_uri="http://localhost:7437", api_key=os.getenv("AGIXT_API_KEY", None)
)


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def nlp(text):
    try:
        sp = spacy.load("en_core_web_sm")
    except:
        spacy.cli.download("en_core_web_sm")
        sp = spacy.load("en_core_web_sm")
    sp.max_length = 99999999999999999999999
    return sp(text)


def camel_to_snake(camel_str):
    camel_str = camel_str.replace(" ", "")
    snake_str = ""
    for i, char in enumerate(camel_str):
        if char.isupper():
            if i != 0 and camel_str[i - 1].islower():
                snake_str += "_"
            if i != len(camel_str) - 1 and camel_str[i + 1].islower():
                snake_str += "_"
        snake_str += char.lower()
    snake_str = snake_str.strip("_")
    return snake_str


def chroma_compute_similarity_scores(
    embedding: ndarray, embedding_array: ndarray, logger=None
) -> ndarray:
    query_norm = linalg.norm(embedding)
    collection_norm = linalg.norm(embedding_array, axis=1)
    valid_indices = (query_norm != 0) & (collection_norm != 0)
    similarity_scores = array([-1.0] * embedding_array.shape[0])

    if valid_indices.any():
        similarity_scores[valid_indices] = embedding.dot(
            embedding_array[valid_indices].T
        ) / (query_norm * collection_norm[valid_indices])
        if not valid_indices.all() and logger:
            logger.warning(
                "Some vectors in the embedding collection are zero vectors."
                "Ignoring cosine similarity score computation for those vectors."
            )
    else:
        raise ValueError(
            f"Invalid vectors, cannot compute cosine similarity scores"
            f"for zero vectors"
            f"{embedding_array} or {embedding}"
        )
    return similarity_scores


def query_results_to_records(results: "QueryResult"):
    try:
        if isinstance(results["ids"][0], str):
            for k, v in results.items():
                results[k] = [v]
    except IndexError:
        return []
    memory_records = [
        {
            "is_reference": metadata["is_reference"] == "True",
            "external_source_name": metadata["external_source_name"],
            "id": metadata["id"],
            "description": metadata["description"],
            "text": document,
            "embedding": embedding,
            "additional_metadata": metadata["additional_metadata"],
            "key": id,
            "timestamp": metadata["timestamp"],
        }
        for id, document, embedding, metadata in zip(
            results["ids"][0],
            results["documents"][0],
            results["embeddings"][0],
            results["metadatas"][0],
        )
    ]
    return memory_records


class Memories:
    def __init__(
        self, agent_name: str = "AGiXT", agent_config=None, collection_number: int = 0
    ):
        self.agent_name = agent_name
        self.collection_name = camel_to_snake(agent_name)
        if collection_number > 0:
            self.collection_name = f"{self.collection_name}_{collection_number}"
        if agent_config is None:
            agent_config = ApiClient.get_agentconfig(agent_name=agent_name)
        self.agent_config = (
            agent_config if agent_config else {"settings": {"embedder": "default"}}
        )
        self.agent_settings = (
            self.agent_config["settings"]
            if "settings" in self.agent_config
            else {"embedder": "default"}
        )
        memories_dir = os.path.join(os.getcwd(), "memories")
        if not os.path.exists(memories_dir):
            os.makedirs(memories_dir)
        self.chroma_client = chromadb.PersistentClient(
            path=memories_dir,
            settings=Settings(
                anonymized_telemetry=False,
            ),
        )
        self.embed = Embedding(AGENT_CONFIG=self.agent_config)
        self.chunk_size = self.embed.chunk_size
        self.embedder = self.embed.embedder

    async def wipe_memory(self):
        self.chroma_client.delete_collection(name=self.collection_name)

    async def get_collection(self):
        try:
            return self.chroma_client.get_collection(
                name=self.collection_name, embedding_function=self.embedder
            )
        except:
            self.chroma_client.create_collection(
                name=self.collection_name,
                embedding_function=self.embedder,
            )
            return self.chroma_client.get_collection(
                name=self.collection_name, embedding_function=self.embedder
            )

    async def delete_memory(self, key: str):
        collection = await self.get_collection()
        try:
            collection.delete(ids=key)
            return True
        except:
            return False

    async def write_text_to_memory(self, user_input: str, text: str):
        collection = await self.get_collection()
        if text:
            if not isinstance(text, str):
                text = str(text)
            chunks = await self.chunk_content(content=text, chunk_size=self.chunk_size)
            for chunk in chunks:
                metadata = {
                    "timestamp": datetime.now().isoformat(),
                    "is_reference": str(False),
                    "external_source_name": user_input,
                    "description": user_input,
                    "additional_metadata": chunk,
                    "id": sha256(
                        (chunk + datetime.now().isoformat()).encode()
                    ).hexdigest(),
                }
                collection.add(
                    ids=metadata["id"],
                    metadatas=metadata,
                    documents=chunk,
                )

    async def get_memories_data(
        self,
        user_input: str,
        limit: int,
        min_relevance_score: float = 0.0,
    ):
        if not user_input:
            return ""
        collection = await self.get_collection()
        if collection == None:
            return ""
        embedding = array(self.embed.embed_text(text=user_input))
        results = collection.query(
            query_embeddings=embedding.tolist(),
            n_results=limit,
            include=["embeddings", "metadatas", "documents"],
        )
        embedding_array = array(results["embeddings"][0])
        if len(embedding_array) == 0:
            logging.warning("Embedding collection is empty.")
            return []
        embedding_array = embedding_array.reshape(embedding_array.shape[0], -1)
        if len(embedding.shape) == 2:
            embedding = embedding.reshape(
                embedding.shape[1],
            )
        similarity_score = chroma_compute_similarity_scores(
            embedding=embedding, embedding_array=embedding_array
        )
        record_list = [
            (record, distance)
            for record, distance in zip(
                query_results_to_records(results=results),
                similarity_score,
            )
        ]
        sorted_results = sorted(
            record_list,
            key=lambda x: x[1],
            reverse=True,
        )
        filtered_results = [x for x in sorted_results if x[1] >= min_relevance_score]
        top_results = filtered_results[:limit]
        return top_results

    async def get_memories(
        self,
        user_input: str,
        limit: int,
        min_relevance_score: float = 0.0,
    ) -> List[str]:
        results = await self.get_memories_data(
            user_input=user_input,
            limit=limit,
            min_relevance_score=min_relevance_score,
        )
        response = []
        if results:
            for result in results:
                metadata = result[0]["additional_metadata"]
                if metadata not in response:
                    response.append(metadata)
        return response

    def score_chunk(self, chunk: str, keywords: set) -> int:
        """Score a chunk based on the number of query keywords it contains."""
        chunk_counter = Counter(chunk.split())
        score = sum(chunk_counter[keyword] for keyword in keywords)
        return score

    async def chunk_content(self, content: str, chunk_size: int) -> List[str]:
        doc = nlp(content)
        sentences = list(doc.sents)
        content_chunks = []
        chunk = []
        chunk_len = 0
        keywords = [
            token.text for token in doc if token.pos_ in {"NOUN", "PROPN", "VERB"}
        ]
        for sentence in sentences:
            sentence_tokens = len(sentence)
            if chunk_len + sentence_tokens > chunk_size and chunk:
                chunk_text = " ".join(token.text for token in chunk)
                content_chunks.append(
                    (self.score_chunk(chunk_text, keywords), chunk_text)
                )
                chunk = []
                chunk_len = 0

            chunk.extend(sentence)
            chunk_len += sentence_tokens

        if chunk:
            chunk_text = " ".join(token.text for token in chunk)
            content_chunks.append((self.score_chunk(chunk_text, keywords), chunk_text))

        # Sort the chunks by their score in descending order before returning them
        content_chunks.sort(key=lambda x: x[0], reverse=True)
        return [chunk_text for score, chunk_text in content_chunks]
