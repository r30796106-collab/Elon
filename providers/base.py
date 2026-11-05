from abc import ABC, abstractmethod


class AIProvider(ABC):
    @abstractmethod
    def prepare(self, task):
        pass
