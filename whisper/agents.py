"""
Agent system: Named, routable agents within each node.

Each node hosts logical agents (not separate processes) with:
- ENS identity (e.g., ml-agent.node1.axl.eth)
- Domain keywords for routing
- Specialized system prompts
- Execution method (calls Ollama with domain context)
"""

from typing import Optional
import logging

logger = logging.getLogger(__name__)


class Agent:
    """A logical agent with ENS identity and domain expertise."""

    def __init__(
        self,
        ens_name: str,
        domain: str,
        keywords: list[str],
        system_prompt: str,
    ):
        """
        Args:
            ens_name: Full ENS name (e.g., "ml-agent.node1.axl.eth")
            domain: Domain type (e.g., "ai-ml", "web3", "devops")
            keywords: List of keywords for routing (lowercase)
            system_prompt: Specialized system prompt for this agent
        """
        self.ens_name = ens_name
        self.domain = domain
        self.keywords = [kw.lower() for kw in keywords]
        self.system_prompt = system_prompt

    def matches_query(self, query: str) -> int:
        """Score how well this agent matches a query."""
        query_lower = query.lower()
        return sum(1 for kw in self.keywords if kw in query_lower)

    def __repr__(self) -> str:
        return f"<Agent {self.ens_name} ({self.domain})>"


class AgentRegistry:
    """Registry of agents within a node."""

    def __init__(self, node_id: int, node_ens_name: str):
        """
        Args:
            node_id: Shard ID (1-6)
            node_ens_name: Node's ENS name (e.g., "node1.axl.eth")
        """
        self.node_id = node_id
        self.node_ens_name = node_ens_name

        # Define the 3 agents for this node
        self.agents = {
            "ml": Agent(
                ens_name=f"ml-agent.{node_ens_name}",
                domain="ai-ml",
                keywords=[
                    "machine learning",
                    "neural",
                    "transformer",
                    "ai",
                    "deep learning",
                    "model",
                    "training",
                    "dataset",
                    "algorithm",
                    "network",
                ],
                system_prompt="""You are an expert in Machine Learning, Artificial Intelligence, and Deep Learning.
You specialize in neural networks, transformers, optimization, loss functions, and data science.
Provide technical depth, recent research insights, and practical examples.
Reference specific papers and architectures when relevant.""",
            ),
            "web3": Agent(
                ens_name=f"web3-agent.{node_ens_name}",
                domain="web3",
                keywords=[
                    "smart contract",
                    "solidity",
                    "evm",
                    "defi",
                    "ethereum",
                    "web3",
                    "dapp",
                    "blockchain",
                    "token",
                    "flash loan",
                ],
                system_prompt="""You are an expert in Web3, Smart Contracts, and Blockchain Technology.
You specialize in Solidity, EVM, DeFi protocols, tokenomics, and Ethereum architecture.
Provide code examples, security considerations, and implementation details.
Focus on practical patterns and real-world DeFi applications.""",
            ),
            "devops": Agent(
                ens_name=f"devops-agent.{node_ens_name}",
                domain="devops",
                keywords=[
                    "kubernetes",
                    "docker",
                    "ci/cd",
                    "devops",
                    "deployment",
                    "pipeline",
                    "automation",
                    "monitoring",
                    "observability",
                    "infrastructure",
                ],
                system_prompt="""You are an expert in DevOps, Infrastructure as Code, and CI/CD.
You specialize in Kubernetes, Docker, automation frameworks, monitoring, and observability.
Provide practical implementations, best practices, and production-ready patterns.
Include examples of configuration, scripts, and architectural decisions.""",
            ),
        }

    def find_best_agent(self, query: str) -> Agent:
        """
        Find the agent that best matches the query.

        Returns the agent with the highest keyword match score.
        If multiple agents have the same score, returns the first one.
        If no agent matches, returns the default ml agent.
        """
        scores = {}
        for agent_id, agent in self.agents.items():
            score = agent.matches_query(query)
            scores[agent_id] = score

        best_agent_id = max(scores, key=scores.get)

        # If no keywords matched, default to ml agent
        if scores[best_agent_id] == 0:
            best_agent_id = "ml"

        return self.agents[best_agent_id]

    def execute_with_agent(self, query: str, ollama_fn) -> tuple[str, str]:
        """
        Execute a query using the best matching agent.

        Args:
            query: The user's query
            ollama_fn: Function that calls Ollama (takes query and system_prompt, returns result)

        Returns:
            Tuple of (result, agent_ens_name)
        """
        agent = self.find_best_agent(query)

        try:
            result = ollama_fn(query, agent.system_prompt)
            logger.info(
                f"Agent {agent.ens_name} executed query (match score: {agent.matches_query(query)})"
            )
            return result, agent.ens_name
        except Exception as e:
            logger.error(f"Agent {agent.ens_name} execution failed: {e}")
            raise

    def get_agent_by_ens_name(self, ens_name: str) -> Optional[Agent]:
        """Get an agent by its ENS name."""
        for agent in self.agents.values():
            if agent.ens_name == ens_name:
                return agent
        return None

    def list_agents(self) -> list[dict]:
        """Return list of all agents with their metadata."""
        return [
            {
                "ens_name": agent.ens_name,
                "domain": agent.domain,
                "keywords": agent.keywords,
            }
            for agent in self.agents.values()
        ]

    def __repr__(self) -> str:
        agents_str = ", ".join(a.ens_name for a in self.agents.values())
        return f"<AgentRegistry {self.node_ens_name}: [{agents_str}]>"
