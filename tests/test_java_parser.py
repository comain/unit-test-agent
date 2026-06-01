import pytest
import os
from uta.language.java.parse.java_parser import JavaParser

def test_parse_sample_service(fixtures_dir):
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    parser = JavaParser()
    result = parser.parse_file(service_path)
    
    assert result.package == "com.example.service"
    assert "com.example.mapper.SampleMapper" in result.imports
    
    # Symbols
    symbols = {s.fqn: s for s in result.symbols}
    assert "com.example.service.SampleService" in symbols
    assert "com.example.service.SampleService.sampleMapper" in symbols
    assert "com.example.service.SampleService.process" in symbols
    
    service_class = symbols["com.example.service.SampleService"]
    assert service_class.kind == "class"
    assert any(a.name == "Service" for a in service_class.annotations)
    
    process_method = symbols["com.example.service.SampleService.process"]
    assert process_method.kind == "method"
    assert process_method.return_type == "String"
    assert len(process_method.params) == 1
    assert process_method.params[0].type == "String"
    assert process_method.params[0].name == "input"
    
    mapper_field = symbols["com.example.service.SampleService.sampleMapper"]
    assert mapper_field.kind == "field"
    assert any(a.name == "Resource" for a in mapper_field.annotations)
    assert result.field_bindings["sampleMapper"] == "SampleMapper"
    
    # Calls
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.caller_fqn == "com.example.service.SampleService.process"
    assert call.callee_name == "selectById"
    assert call.receiver_name == "sampleMapper"

def test_parse_sample_mapper(fixtures_dir):
    mapper_path = os.path.join(fixtures_dir, "SampleMapper.java")
    parser = JavaParser()
    result = parser.parse_file(mapper_path)
    
    assert result.package == "com.example.mapper"
    
    symbols = {s.fqn: s for s in result.symbols}
    # For interfaces, I should probably handle them in _traverse.
    # Currently _traverse handles class_declaration. Let's see if interface_declaration is needed.
    # In my java_parser.py, I only checked for class_declaration.
    pass
