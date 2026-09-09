import asyncio
import json
from pathlib import Path
import sys

from harness.cli import build_kernel
from harness.improvement import verdict
from harness.improvement_journal import read_improvements
from harness.permissions import PermissionEngine, PermissionRule, RuleSet
from harness.provider_litellm import CatalogProvider
from harness.semantic_assessment import AssessmentPrompt
from scripts.qualify_assessment_evaluation import public_experiments
from scripts.qualify_local import catalog, close, isolation

index = sys.argv[1]
out = Path('/reports') / ('development-' + index + (sys.argv[2] if len(sys.argv) > 2 else ''))
out.mkdir()
models = catalog(Path('/models/8b.gguf'), 'qwen3-8b')
entry = models.entries['local-small']
entry['max_input_tokens'] = 4096
entry['local']['startup_seconds'] = 120
command = entry['local']['command']
command[command.index('--n-gpu-layers') + 1] = '28'
command[command.index('--ctx-size') + 1] = '4096'
command[command.index('--presence-penalty') + 1] = '0'
spec = public_experiments()[0]
spec = spec.model_copy(update={
    'candidate': AssessmentPrompt.model_validate_json((Path('/reports') / f'candidate-{index}.json').read_bytes()),
    'configuration': spec.configuration.model_copy(update={'runtime_version':'llama.cpp-b9603-qwen3-8b-gpu28-c4096-presence0'}),
    'hypothesis': 'An ordered eligibility and ambiguity procedure corrects prior public selection failures.',
    'expected_benefit': 'Correct unavailable-context and ambiguous-reference responses without losing semantic relevance.',
})
(out/'frozen-experiment.json').write_text(spec.model_dump_json(indent=2)+'\n')
(out/'catalog.json').write_text(json.dumps(models.entries,indent=2)+'\n')
report = {'stage':'development','held_out':False,'isolation':isolation()}

async def run():
    kernel = build_kernel(base_dir=out/'state',model=spec.configuration.model,provider=CatalogProvider(models),
        permissions=PermissionEngine([RuleSet(rules=[PermissionRule('allow','model:local-small')],default='deny')]))
    try:
        await kernel.loop.start()
        async with kernel.resources.use(models.resolve('local-small'), emit=kernel.session.append):
            pass
        for case in spec.suite.cases[1:4]:
            observation = await kernel.semantics.select_context(case.input,model=spec.configuration.model)
            print('seed',case.id,observation.reason,flush=True)
        result = await kernel.improvement_service.compare_assessment(spec)
        state = read_improvements(kernel.session.base,kernel.session.id)
        plan = state.plans[result.plan_id]
        report.update(verdict=verdict(plan,result),plan=plan.model_dump(mode='json'),result=result.model_dump(mode='json'),
            report=json.loads(kernel.session.blobs.get(result.artifact)),session_id=str(kernel.session.id))
        for c in report['report']['cases']:
            print(c['id'], {side:(c[side]['reason'], c[side]['result']) for side in ('incumbent','candidate')},flush=True)
        print('verdict',report['verdict'],'metrics',report['report']['metrics'],flush=True)
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        raise
    finally:
        await close(kernel)
        report['owned_runtime_stopped']=not kernel.resources._owned
        (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')

asyncio.run(run())
