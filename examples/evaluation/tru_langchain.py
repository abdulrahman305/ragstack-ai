import tru_shared
from langchain.prompts import ChatPromptTemplate
from langchain.schema import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

framework = tru_shared.Framework.LANG_CHAIN

collection_name = "open_ai_512"

vstore = tru_shared.get_astra_vector_store(framework, collection_name)
chat_model = tru_shared.get_azure_chat_model(framework, "gpt-35-turbo", "0613")
embeddings = tru_shared.get_azure_embeddings_model(framework)

prompt_template = """
Answer the question based only on the supplied context. If you don't know the answer, say: "I don't know".
Context: {context}
Question: {question}
Your answer:
"""  # noqa: E501
prompt = ChatPromptTemplate.from_template(prompt_template)

pipeline = (
    {"context": vstore.as_retriever(), "question": RunnablePassthrough()}
    | prompt
    | chat_model
    | StrOutputParser()
)

tru_shared.execute_experiment(framework, pipeline, "lc_512")
