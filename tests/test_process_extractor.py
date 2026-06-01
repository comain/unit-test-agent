import pytest
import os
from uta.language.java.parse.java_parser import JavaParser
from uta.language.java.parse.graph_builder import GraphBuilder
from uta.language.java.parse.process_extractor import ProcessExtractor

def test_extract_flows(fixtures_dir):
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    mapper_path = os.path.join(fixtures_dir, "SampleMapper.java")
    biz_path = os.path.join(fixtures_dir, "SampleBizImpl.java")
    
    results = [
        parser.parse_file(service_path),
        parser.parse_file(mapper_path),
        parser.parse_file(biz_path)
    ]
    
    builder = GraphBuilder()
    graph = builder.build(results)
    
    extractor = ProcessExtractor(graph)
    flows = extractor.extract_flows(["com.example.biz.SampleBizImpl.execute"])
    
    assert len(flows) == 1
    flow = flows[0]
    assert flow.entry_point == "com.example.biz.SampleBizImpl.execute"
    
    # Steps should be: execute -> process -> selectById
    steps = [s.fqn for s in flow.steps]
    assert "com.example.biz.SampleBizImpl.execute" in steps
    assert "com.example.service.SampleService.process" in steps
    assert "com.example.mapper.SampleMapper.selectById" in steps
    
    # Check kinds
    kinds = {s.fqn: s.kind for s in flow.steps}
    assert kinds["com.example.mapper.SampleMapper.selectById"] == "db_query"
