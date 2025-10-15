import json
import os

import requests
from typing import Dict, List, Any

from evaluator.components.data_provider import QuerySpecification
from evaluator.config.schema import ModelConfig
from evaluator.interfaces.metric_collector import MetricCollector
from evaluator.utils.module_extractor import register_metric_collector
from evaluator.utils.utils import log_verbose, extract_final_answer_from_response, strip_think, log


@register_metric_collector("fac_metric_collector")
class FACMetricCollector(MetricCollector):
    """
    Final Answer Correctness (FAC) Metric Collector
    
    Collects FAC metrics by directly calling a remote judge model
    with the StableToolBench FAC judge prompt.
    """

    FAC_JUDGE_PROMPT = """
Given a query and an answer provided by an AI agent, you now need to determine the answer_status of whether the well solved the query, i.e. whether the need of the query is satisfied. You need to output "Unsolved" or "Solved" and your reason. You must obey the following rules:

You should response "Solved" when:
    1. If the answer well provides the information needed by the query, then it is "Solved". The answer does not need to be perfect, and it only needs to make a genuine attempt to address the query.
	2.	Consider only Completeness:
	    •	The answer attempts to address every part of the query, regardless of whether the information provided is factually correct or accurate, unless there is a severe factual error.
	3.	For Multi-part Queries:
	    •	For queries with multiple parts, all parts must be addressed for the answer to be considered “Solved”.
	4.	Genuine Attempt :
	    •	The answer makes a genuine attempt to provide the requested information or perform the requested task for all parts of the query. This includes scenarios where the answer concludes that “nothing” is a reasonable response (e.g., when the requested information does not exist or is not available, or a possible answer of the query is nothing and the model answers nothing after reasonable attempts).

You should response "Unsolved" when:
    1.	Refusal, Apology, or Non-engagement:
	    •	The answer includes a refusal or apology (e.g., “I'm sorry, I can't help with that”).
	    •	The answer does not directly engage with or address the query in any way.
	2.	Multi-part Queries:
	    •	If the query has multiple parts and at least one part is not well addressed.
    3. Severe Factual Error:
        •   If the answer contains a severe factual error that significantly impacts the usefulness of the information provided.


Additional Guidelines:
    1. VERY IMPORTANT: DO NOT BE TOO HARSH. The model does not need to be perfect, and the answer does not need to be flawless. It only needs to make a genuine attempt to address the query.
    1. DO NOT evaluate factual accuracy or correctness of the information provided based on your knowledge. Assume that the information provided is accurate and focus solely on whether the answer attempts to address all parts of the query, unless there is a severe factual error that conficts common knowledge.
	2.	Focus on Final Answer: Only the final answer is provided and should be considered, disregarding any processes that were used to generate the answer. You only need to judge whether the information need is satisfied.
	3.	Answer Completion: The agent does not need to detail how it arrived at the answer, only that the answer itself is complete and attempts to address the query.

Here are some examples:

Query 1: I'm planning a movie night with my friends and we want to watch a horror film. Can you recommend some popular horror movies available on streaming platforms in the US? Also, provide the runtime and IMDb ratings for these movies.
Answer 1: Here are some popular horror movies available on streaming platforms in the US:\n\n1. Knives Out\n   - Runtime: 130 minutes\n   - IMDb Rating: 7.9/10\n   - Available on: Netflix, Prime Video, Hulu, Amazon Prime Video\n\n2. Jumanji: The Next Level\n   - Runtime: 110 minutes\n   - IMDb Rating: 6.7/10\n   - Available on: Hulu, Amazon Prime Video, Netflix\n\nPlease note that availability may vary depending on your location and streaming platform subscriptions. Enjoy your movie night!
Answer Status: Solved
Reason: The answer addressed all parts of subqueries by providing a list of popular horror movies available on streaming platforms in the US, along with their runtime and IMDb ratings. Whether the film is horror is a factual matter that does not to be checked.

Query 2: I'm a screenwriter looking for inspiration for my next project. Can you help me find a list of critically acclaimed movies available on streaming platforms in the US? Also, provide me with the streaming sources for the movie 'Citizen Kane' and the basic information about the cast, including their names and professions.
Answer 2: The movie 'Citizen Kane' is available on the following streaming platforms:
- HBO Max: [Watch on HBO Max](https://play.hbomax.com/feature/urn:hbo:feature:GXduU_gwwz-NvjAEAAAAC)
- Amazon Prime Video: [Watch on Amazon Prime Video](https://www.amazon.com/Citizen-Kane-Orson-Welles/dp/B000I9YLWG)
- AppleTV+: [Watch on AppleTV+](https://tv.apple.com/us/movie/citizen-kane/umc.cmc.21zj0v11gnqbbqrebieh0vpk7)
- Disney+: Available on Disney+
- Netflix: Available on Netflix
Answer Status: Unsolved
Reason: The answer only addressed the first and second part of the query by providing the streaming sources for the movie 'Citizen Kane' but did not provide information about the cast or a list of critically acclaimed movies available on streaming platforms in the US. The response was incomplete and did not fully address the query.

Query: 
{query}
Answer: 
{answer}

Now give your reason and answer status in the following format:

Answer Status
xxx (can only be "Solved" or "Unsolved")
Reason
xxx
"""

    def __init__(self, settings: Dict, model_config: List[ModelConfig]):
        super().__init__(settings, model_config)

        # Metrics storage
        self.query_results = None

        # judge model configuration
        self.judge_model_url = os.getenv('FAC_JUDGE_MODEL_URL')
        if not self.judge_model_url:
            raise ValueError("FAC_JUDGE_MODEL_URL environment variable is required")

    def get_collected_metrics_names(self) -> List[str]:
        return [
            "Average Task Success (FAC Evaluator)"
        ]

    def set_up(self) -> None:
        """Initialize the FAC metric collector."""
        super().set_up()
        self.query_results = []

    def prepare_for_measurement(self, query_spec: QuerySpecification) -> None:
        """Prepare for measuring a single query."""
        pass

    def register_measurement(self, query_spec: QuerySpecification, response: Any = None, **kwargs) -> None:
        """Register measurement for a single query."""
        try:
            query = query_spec.query

            # Extract final answer from algorithm response
            final_answer = extract_final_answer_from_response(response)
            final_answer = strip_think(final_answer)

            # Evaluate using LLM judge model
            evaluation_result = self.evaluate_with_llm_judge(query, final_answer)

            status_emoji = "✅" if evaluation_result["is_solved"] else "❌"

            # Always store only the boolean for memory efficiency
            self.query_results.append(evaluation_result["is_solved"])

            log_verbose(f"📊 FAC Query Evaluated: {query[:50]}... {status_emoji}")
            # Show detailed judge model output
            self.log_judge_output(query, final_answer, evaluation_result["evaluation"])

        except Exception as e:
            # Store False for failed evaluations
            self.query_results.append(False)

    def evaluate_with_llm_judge(self, query: str, answer: str) -> Dict[str, Any]:
        """Evaluate query-answer pair using LLM judge model."""
        try:
            # Format the prompt
            prompt = self.FAC_JUDGE_PROMPT.format(query=query, answer=answer)

            # Prepare payload for llm judge model
            payload = {
                "prompt": prompt,
                "max_new_tokens": 512,
                "do_sample": False,
                "top_p": 1.0
            }

            # Call judge model
            response = requests.post(
                self.judge_model_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()

                # Extract generated text from response
                generated_text = ""
                if "generated_text" in result:
                    generated_text = result["generated_text"]
                elif "text" in result:
                    generated_text = result["text"]
                elif "response" in result:
                    generated_text = result["response"]
                elif "output" in result:
                    generated_text = result["output"]
                else:
                    generated_text = str(result)

                # Extract only the evaluation part (look for the last "Answer Status:" in the response)
                if "Answer Status:" in generated_text:
                    # Find the last occurrence of "Answer Status:" to get the actual evaluation
                    last_evaluation_start = generated_text.rfind("Answer Status:")
                    generated_text = generated_text[last_evaluation_start:]

                # Parse the evaluation to determine if solved
                is_solved = self.parse_evaluation_result(generated_text)

                return {
                    "evaluation": generated_text,
                    "is_solved": is_solved
                }
            else:
                error_msg = f"API call failed: {response.status_code} - {response.text}"
                log(f"❌ LLM judge model API error: {error_msg}")
                log(f"🔍 Debug info:")
                log(f"   URL: {self.judge_model_url}")
                log(f"   Payload: {json.dumps(payload, indent=2)}")
                log(f"   Response: {response.text}")
                return {
                    "evaluation": f"Answer Status: Unsolved\nReason: {error_msg}",
                    "is_solved": False
                }

        except Exception as e:
            error_msg = f"Error calling LLM judge model: {e}"
            log(f"❌ {error_msg}")
            log(f"🔍 Debug info:")
            log(f"   URL: {self.judge_model_url}")
            log(f"   Error: {e}")
            return {
                "evaluation": f"Answer Status: Unsolved\nReason: {error_msg}",
                "is_solved": False
            }

    @staticmethod
    def parse_evaluation_result(evaluation_text: str) -> bool:
        """Parse evaluation text to determine if the query was solved."""
        try:
            # Convert to lowercase for case-insensitive matching
            text = evaluation_text.lower().strip()

            # Look for "solved" or "unsolved" in the text
            if "unsolved" in text:
                return False
            elif "solved" in text:
                return True
            else:
                # If unclear, default to unsolved (conservative approach)
                log(f"⚠️ Unclear evaluation result: {evaluation_text[:100]}...")
                return False

        except Exception as e:
            log(f"❌ Error parsing evaluation result: {e}")
            return False

    @staticmethod
    def log_judge_output(query: str, answer: str, evaluation: str):
        """Log detailed judge model output and explanation."""
        log_verbose(f"\n{'=' * 60}")
        log_verbose(f"🔍 JUDGE MODEL EVALUATION")
        log_verbose(f"{'=' * 60}")

        log_verbose(f"📝 Query: {query}")
        log_verbose(f"💬 Agent's Answer: {answer}")
        log_verbose(f"\n📋 Judge Model Output:")
        log_verbose(f"{'-' * 40}")
        log_verbose(evaluation)
        log_verbose(f"{'-' * 40}")

    def tear_down(self) -> None:
        """Clean up after all measurements."""
        super().tear_down()

    def report_results(self) -> Dict[str, Any]:
        """Report the collected metrics."""

        total_queries = len(self.query_results)
        # Count solved queries from our direct evaluations
        solved_queries = sum(1 for is_solved in self.query_results if is_solved)
        solve_rate = solved_queries / total_queries if total_queries > 0 else 0.0

        log(f"Average Task Success (FAC Evaluator): {solve_rate:.2f} (Solved {solved_queries}/{total_queries} queries)")

        results = {
            "Average Task Success (FAC Evaluator)": solve_rate
        }

        return results
