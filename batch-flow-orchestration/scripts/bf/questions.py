"""User decision boundary. Technical trouble alone is never a user approval request."""
CATEGORIES = {
    'commitment': '改变交付承诺', 'authority': '扩大授权边界', 'budget': '增加预算或资源投入',
    'risk': '接受残余风险或新责任', 'information': '缺少不可代替的用户信息或选择',
    'checkpoint': '用户明确设置的确认点',
}


def validate(request):
    if not isinstance(request, dict) or request.get('category') not in CATEGORIES:
        return '用户问题须属于 commitment/authority/budget/risk/information/checkpoint 六类之一'
    for key in ('decision', 'recommendation', 'impact'):
        if not isinstance(request.get(key), str) or not request[key].strip():
            return '用户问题须说明 decision、recommendation 和 impact'
    return ''


def for_actions(actions, diagnosis):
    categories = []
    decisions = []
    for a in actions:
        category = ('budget' if 'grant' in a else 'commitment' if 'plan_revision' in a or 'supersede' in a
                    else 'authority')
        if category not in categories:
            categories.append(category)
        if 'grant' in a:
            decisions.append('追加额度：%s' % a['grant'])
        elif 'authorize' in a:
            decisions.append('给 %s 授权精确路径：%s' % (a['authorize'], '、'.join(a['paths'])))
        elif 'plan_revision' in a:
            decisions.append('批准计划变更：%s' % a['plan_revision'])
        elif 'supersede' in a:
            decisions.append('停用 %s：%s' % (a['supersede'], a.get('reason', '见负责人诊断')))
        else:
            decisions.append('扩大 %s 的实施范围或保护路径权限' % a.get('target', '本项'))
    return {'category': categories[0], 'categories': categories,
            'decision': '；'.join(decisions),
            'recommendation': diagnosis or '按所列限定动作恢复任务',
            'impact': '批准后仅执行所列动作；拒绝则保留相关阻塞并交负责人调整方案，不影响独立路径。'}
