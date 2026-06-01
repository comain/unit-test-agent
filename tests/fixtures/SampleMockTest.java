package com.example.service;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.runners.MockitoJUnitRunner;

import static org.junit.Assert.assertEquals;
import static org.mockito.Mockito.when;

@RunWith(MockitoJUnitRunner.class)
public class SampleServiceTest {

    @Mock
    private SampleMapper sampleMapper;

    @InjectMocks
    private SampleService sampleService;

    @Before
    public void setUp() {
    }

    @Test
    public void testProcess() {
        when(sampleMapper.selectById("1")).thenReturn("name1");
        String result = sampleService.process("1");
        assertEquals("name1", result);
    }
}
