name: 功能建议
about: 提议一个新功能或改进
labels: enhancement
body:
  - type: textarea
    id: problem
    attributes:
      label: 要解决什么问题
      description: 当前遇到的痛点或缺失的能力
    validations:
      required: true
  - type: textarea
    id: solution
    attributes:
      label: 期望的方案
      description: 你希望它如何工作
    validations:
      required: true
  - type: dropdown
    id: area
    attributes:
      label: 涉及模块
      options:
        - 后端 API / 运行时
        - 平台适配（platforms/）
        - 前端界面
        - 部署 / 运维
        - 其他
