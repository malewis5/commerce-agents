set -x
pwd
ls -a
mkdir -p _bundle && echo hello > _bundle/marker.txt
python -c "import sys; print(sys.executable)"
python -c "import shopping_agent, demo_common; print('imports ok')" || echo IMPORTS-FAIL
