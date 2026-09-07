# llm-judge Lambda 컨테이너 이미지. Bedrock invoke_model로 에이전트 위협 판정.
FROM public.ecr.aws/lambda/python:3.12

RUN pip install --no-cache-dir boto3

# shared normalize + llm-judge handler + rubric. (빌드 컨텍스트는 infra/scan-tools/ 로 잡아 shared를 COPY.)
COPY shared/normalize.py ${LAMBDA_TASK_ROOT}/normalize.py
COPY llm-judge/handler.py ${LAMBDA_TASK_ROOT}/handler.py
COPY llm-judge/rubric.py ${LAMBDA_TASK_ROOT}/rubric.py

CMD ["handler.handler"]
