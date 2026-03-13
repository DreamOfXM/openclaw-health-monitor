#!/usr/bin/env python3
"""
测试运行脚本
"""
import sys
import os
import unittest

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tests'))

# 导入测试模块
from tests import test_health_score
from tests import test_data_collector
from tests import test_api_routes

# 创建测试套件
loader = unittest.TestLoader()
suite = unittest.TestSuite()

# 添加测试
suite.addTests(loader.loadTestsFromModule(test_health_score))
suite.addTests(loader.loadTestsFromModule(test_data_collector))
suite.addTests(loader.loadTestsFromModule(test_api_routes))

print(f"✅ 加载测试模块: test_health_score ({suite.countTestCases()} 个测试)")

# 运行测试
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

# 输出总结
print("\n" + "="*70)
print("测试总结")
print("="*70)
print(f"总测试数: {result.testsRun}")
print(f"通过: {result.testsRun - len(result.failures) - len(result.errors)}")
print(f"失败: {len(result.failures)}")
print(f"错误: {len(result.errors)}")

if result.failures:
    print("\n失败的测试:")
    for test, traceback in result.failures:
        print(f"  - {test}")

if result.errors:
    print("\n错误的测试:")
    for test, traceback in result.errors:
        print(f"  - {test}")

# 返回退出码
sys.exit(0 if result.wasSuccessful() else 1)