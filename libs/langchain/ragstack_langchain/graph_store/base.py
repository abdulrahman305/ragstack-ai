from __future__ import annotations

from abc import abstractmethod
from typing import (
    Any,
    AsyncIterable,
    ClassVar,
    Collection,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
)

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.load import Serializable
from langchain_core.runnables import run_in_executor
from langchain_core.vectorstores import VectorStore, VectorStoreRetriever
from langchain_core.pydantic_v1 import Field

from ragstack_langchain.graph_store.links import METADATA_LINKS_KEY, Link


def _has_next(iterator: Iterator) -> bool:
    """Checks if the iterator has more elements.
    Warning: consumes an element from the iterator"""
    sentinel = object()
    return next(iterator, sentinel) is not sentinel


METADATA_CONTENT_ID_KEY = "content_id"


class Node(Serializable):
    """Node in the GraphStore."""

    id: Optional[str] = None
    """Unique ID for the node. Will be generated by the GraphStore if not set."""
    text: str
    """Text contained by the node."""
    metadata: dict = Field(default_factory=dict)
    """Metadata for the node."""
    links: Set[Link] = Field(default_factory=set)
    """Links associated with the node."""


def _texts_to_nodes(
    texts: Iterable[str],
    metadatas: Optional[Iterable[dict]],
    ids: Optional[Iterable[str]],
) -> Iterator[Node]:
    metadatas_it = iter(metadatas) if metadatas else None
    ids_it = iter(ids) if ids else None
    for text in texts:
        try:
            _metadata = next(metadatas_it).copy() if metadatas_it else {}
        except StopIteration:
            raise ValueError("texts iterable longer than metadatas")
        try:
            _id = next(ids_it) if ids_it else None
            _id = _id or _metadata.pop(METADATA_CONTENT_ID_KEY, None)
        except StopIteration:
            raise ValueError("texts iterable longer than ids")

        links = _metadata.pop(METADATA_LINKS_KEY, set())
        if not isinstance(links, Set):
            links = set(links)
        yield Node(
            id=_id,
            metadata=_metadata,
            text=text,
            links=links,
        )
    if ids_it and _has_next(ids_it):
        raise ValueError("ids iterable longer than texts")
    if metadatas_it and _has_next(metadatas_it):
        raise ValueError("metadatas iterable longer than texts")


def _documents_to_nodes(
    documents: Iterable[Document], ids: Optional[Iterable[str]]
) -> Iterator[Node]:
    ids_it = iter(ids) if ids else None
    for doc in documents:
        try:
            _id = next(ids_it) if ids_it else None
            _id = _id or doc.metadata.pop(METADATA_CONTENT_ID_KEY, None)
        except StopIteration:
            raise ValueError("documents iterable longer than ids")
        metadata = doc.metadata.copy()
        links = metadata.pop(METADATA_LINKS_KEY, set())
        if not isinstance(links, Set):
            links = set(links)
        yield Node(
            id=_id,
            metadata=metadata,
            text=doc.page_content,
            links=links,
        )
    if ids_it and _has_next(ids_it):
        raise ValueError("ids iterable longer than documents")


def nodes_to_documents(nodes: Iterable[Node]) -> Iterator[Document]:
    for node in nodes:
        metadata = node.metadata.copy()
        metadata[METADATA_CONTENT_ID_KEY] = node.id
        metadata[METADATA_LINKS_KEY] = {
            # Convert the core `Link` (from the node) back to the local `Link`.
            Link(kind=link.kind, direction=link.direction, tag=link.tag)
            for link in node.links
        }

        yield Document(
            page_content=node.text,
            metadata=metadata,
        )


class GraphStore(VectorStore):
    """A hybrid vector-and-graph graph store.

    Document chunks support vector-similarity search as well as edges linking
    chunks based on structural and semantic properties.
    """

    @abstractmethod
    def add_nodes(
        self,
        nodes: Iterable[Node],
        **kwargs: Any,
    ) -> Iterable[str]:
        """Add nodes to the graph store.

        Args:
            nodes: the nodes to add.
        """

    async def aadd_nodes(
        self,
        nodes: Iterable[Node],
        **kwargs: Any,
    ) -> AsyncIterable[str]:
        """Add nodes to the graph store.

        Args:
            nodes: the nodes to add.
        """
        iterator = iter(await run_in_executor(None, self.add_nodes, nodes, **kwargs))
        done = object()
        while True:
            doc = await run_in_executor(None, next, iterator, done)
            if doc is done:
                break
            yield doc  # type: ignore[misc]

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[Iterable[dict]] = None,
        *,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        nodes = _texts_to_nodes(texts, metadatas, ids)
        return list(self.add_nodes(nodes, **kwargs))

    async def aadd_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[Iterable[dict]] = None,
        *,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        nodes = _texts_to_nodes(texts, metadatas, ids)
        return [_id async for _id in self.aadd_nodes(nodes, **kwargs)]

    def add_documents(
        self,
        documents: Iterable[Document],
        *,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        nodes = _documents_to_nodes(documents, ids)
        return list(self.add_nodes(nodes, **kwargs))

    async def aadd_documents(
        self,
        documents: Iterable[Document],
        *,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        nodes = _documents_to_nodes(documents, ids)
        return [_id async for _id in self.aadd_nodes(nodes, **kwargs)]

    @abstractmethod
    def traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 1,
        **kwargs: Any,
    ) -> Iterable[Document]:
        """Retrieve documents from traversing this graph store.

        First, `k` nodes are retrieved using a search for each `query` string.
        Then, additional nodes are discovered up to the given `depth` from those
        starting nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial search.
                Defaults to 4. Applies to each of the query strings.
            depth: The maximum depth of edges to traverse. Defaults to 1.
        Returns:
            Retrieved documents.
        """

    async def atraversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 1,
        **kwargs: Any,
    ) -> AsyncIterable[Document]:
        """Retrieve documents from traversing this graph store.

        First, `k` nodes are retrieved using a search for each `query` string.
        Then, additional nodes are discovered up to the given `depth` from those
        starting nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial search.
                Defaults to 4. Applies to each of the query strings.
            depth: The maximum depth of edges to traverse. Defaults to 1.
        Returns:
            Retrieved documents.
        """
        iterator = iter(
            await run_in_executor(
                None, self.traversal_search, query, k=k, depth=depth, **kwargs
            )
        )
        done = object()
        while True:
            doc = await run_in_executor(None, next, iterator, done)
            if doc is done:
                break
            yield doc  # type: ignore[misc]

    @abstractmethod
    def mmr_traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        adjacent_k: int = 10,
        lambda_mult: float = 0.5,
        score_threshold: float = float("-inf"),
        **kwargs: Any,
    ) -> Iterable[Document]:
        """Retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch via similarity.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to negative infinity.
        """

    async def ammr_traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        adjacent_k: int = 10,
        lambda_mult: float = 0.5,
        score_threshold: float = float("-inf"),
        **kwargs: Any,
    ) -> AsyncIterable[Document]:
        """Retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch via similarity.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to negative infinity.
        """
        iterator = iter(
            await run_in_executor(
                None,
                self.mmr_traversal_search,
                query,
                k=k,
                fetch_k=fetch_k,
                adjacent_k=adjacent_k,
                depth=depth,
                lambda_mult=lambda_mult,
                score_threshold=score_threshold,
                **kwargs,
            )
        )
        done = object()
        while True:
            doc = await run_in_executor(None, next, iterator, done)
            if doc is done:
                break
            yield doc  # type: ignore[misc]

    def similarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Document]:
        return list(self.traversal_search(query, k=k, depth=0))

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        **kwargs: Any,
    ) -> List[Document]:
        return list(
            self.mmr_traversal_search(
                query, k=k, fetch_k=fetch_k, lambda_mult=lambda_mult, depth=0
            )
        )

    async def asimilarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Document]:
        return [doc async for doc in self.atraversal_search(query, k=k, depth=0)]

    def search(self, query: str, search_type: str, **kwargs: Any) -> List[Document]:
        if search_type == "similarity":
            return self.similarity_search(query, **kwargs)
        elif search_type == "similarity_score_threshold":
            docs_and_similarities = self.similarity_search_with_relevance_scores(
                query, **kwargs
            )
            return [doc for doc, _ in docs_and_similarities]
        elif search_type == "mmr":
            return self.max_marginal_relevance_search(query, **kwargs)
        elif search_type == "traversal":
            return list(self.traversal_search(query, **kwargs))
        elif search_type == "mmr_traversal":
            return list(self.mmr_traversal_search(query, **kwargs))
        else:
            raise ValueError(
                f"search_type of {search_type} not allowed. Expected "
                "search_type to be 'similarity', 'similarity_score_threshold', "
                "'mmr' or 'traversal'."
            )

    async def asearch(
        self, query: str, search_type: str, **kwargs: Any
    ) -> List[Document]:
        if search_type == "similarity":
            return await self.asimilarity_search(query, **kwargs)
        elif search_type == "similarity_score_threshold":
            docs_and_similarities = await self.asimilarity_search_with_relevance_scores(
                query, **kwargs
            )
            return [doc for doc, _ in docs_and_similarities]
        elif search_type == "mmr":
            return await self.amax_marginal_relevance_search(query, **kwargs)
        elif search_type == "traversal":
            return [doc async for doc in self.atraversal_search(query, **kwargs)]
        else:
            raise ValueError(
                f"search_type of {search_type} not allowed. Expected "
                "search_type to be 'similarity', 'similarity_score_threshold', "
                "'mmr' or 'traversal'."
            )

    def as_retriever(self, **kwargs: Any) -> "GraphStoreRetriever":
        """Return GraphStoreRetriever initialized from this GraphStore.

        Args:
            search_type (Optional[str]): Defines the type of search that
                the Retriever should perform.
                Can be "traversal" (default), "similarity", "mmr", or
                "similarity_score_threshold".
            search_kwargs (Optional[Dict]): Keyword arguments to pass to the
                search function. Can include things like:
                    k: Amount of documents to return (Default: 4)
                    depth: The maximum depth of edges to traverse (Default: 1)
                    score_threshold: Minimum relevance threshold
                        for similarity_score_threshold
                    fetch_k: Amount of documents to pass to MMR algorithm (Default: 20)
                    lambda_mult: Diversity of results returned by MMR;
                        1 for minimum diversity and 0 for maximum. (Default: 0.5)
        Returns:
            Retriever for this GraphStore.

        Examples:

        .. code-block:: python

            # Retrieve documents traversing edges
            docsearch.as_retriever(
                search_type="traversal",
                search_kwargs={'k': 6, 'depth': 3}
            )

            # Retrieve more documents with higher diversity
            # Useful if your dataset has many similar documents
            docsearch.as_retriever(
                search_type="mmr",
                search_kwargs={'k': 6, 'lambda_mult': 0.25}
            )

            # Fetch more documents for the MMR algorithm to consider
            # But only return the top 5
            docsearch.as_retriever(
                search_type="mmr",
                search_kwargs={'k': 5, 'fetch_k': 50}
            )

            # Only retrieve documents that have a relevance score
            # Above a certain threshold
            docsearch.as_retriever(
                search_type="similarity_score_threshold",
                search_kwargs={'score_threshold': 0.8}
            )

            # Only get the single most similar document from the dataset
            docsearch.as_retriever(search_kwargs={'k': 1})

        """
        return GraphStoreRetriever(vectorstore=self, **kwargs)


class GraphStoreRetriever(VectorStoreRetriever):
    """Retriever class for GraphStore."""

    vectorstore: GraphStore
    """GraphStore to use for retrieval."""
    search_type: str = "traversal"
    """Type of search to perform. Defaults to "traversal"."""
    allowed_search_types: ClassVar[Collection[str]] = (
        "similarity",
        "similarity_score_threshold",
        "mmr",
        "traversal",
        "mmr_traversal",
    )

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[Document]:
        if self.search_type == "traversal":
            return list(self.vectorstore.traversal_search(query, **self.search_kwargs))
        elif self.search_type == "mmr_traversal":
            return list(
                self.vectorstore.mmr_traversal_search(query, **self.search_kwargs)
            )
        else:
            return super()._get_relevant_documents(query, run_manager=run_manager)

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> List[Document]:
        if self.search_type == "traversal":
            return [
                doc
                async for doc in self.vectorstore.atraversal_search(
                    query, **self.search_kwargs
                )
            ]
        elif self.search_type == "mmr_traversal":
            return [
                doc
                async for doc in self.vectorstore.ammr_traversal_search(
                    query, **self.search_kwargs
                )
            ]
        else:
            return await super()._aget_relevant_documents(
                query, run_manager=run_manager
            )
