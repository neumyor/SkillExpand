"""Trajectory records and retrievers used by the inherited executor."""
from langchain.retrievers import SVMRetriever, KNNRetriever
from .episode import Trajectory


def choose_retriever(key):
    return SVMRetriever if key == 'svm' else KNNRetriever


RETRIEVERS = choose_retriever
