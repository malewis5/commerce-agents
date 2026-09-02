set -x
pwd
ls -a
ls -a ../..
ls ../../commerce-common || echo MISSING-commerce-common
ls ../../shopping-agent/skills || echo MISSING-skills
ls ../../examples/package.json || echo MISSING-workspace
mkdir -p out && echo probe-ok > out/index.html
