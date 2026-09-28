name: Bug 报告
about: 反馈一个缺陷
labels: bug
body:
  - type: textarea
    id: describe
    attributes:
      label: 问题描述
      description: 清晰简洁地描述问题
    validations:
      required: true
  - type: textarea
    id: repro
    attributes:
      label: 复现步骤
      description: |
        1. 打开 '...'
        2. 点击 '...'
        3. 出现 '...'
  - type: textarea
    id: expect
    attributes:
      label: 期望行为 / 实际行为
  - type: textarea
    id: env
    attributes:
      label: 环境信息
      description: OS、Python 版本、部署方式（both/api+worker）、涉及平台
  - type: textarea
    id: logs
    attributes:
      label: 日志片段
      description: logs/ 或日志页中的相关报错（请脱敏）
